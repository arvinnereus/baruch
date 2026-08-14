"""SQLite FTS5 index over all meeting transcripts, notes, and titles.
Shared by: sidebar library search, the local Ask agent, and the MCP server.
Incremental: reindexes a meeting only when its files change."""
import json
import sqlite3
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data" / "meetings"
DB = APP_DIR / "data" / "index.db"


def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
        mid UNINDEXED, title, kind UNINDEXED, speaker, ts_ms UNINDEXED, text,
        tokenize='unicode61')""")
    c.execute("""CREATE TABLE IF NOT EXISTS indexed
        (mid TEXT PRIMARY KEY, stamp REAL)""")
    return c


def _stamp(mdir: Path) -> float:
    s = 0.0
    for f in ("transcript.json", "note.md", "meeting.json"):
        p = mdir / f
        if p.exists():
            s = max(s, p.stat().st_mtime)
    return s


def refresh():
    """Bring the index up to date. Cheap when nothing changed."""
    c = _conn()
    known = dict(c.execute("SELECT mid, stamp FROM indexed"))
    seen = set()
    for mdir in DATA.iterdir():
        mf = mdir / "meeting.json"
        if not mf.exists():
            continue
        mid = mdir.name
        seen.add(mid)
        stamp = _stamp(mdir)
        if known.get(mid) == stamp:
            continue
        meta = json.loads(mf.read_text(encoding="utf-8"))
        title = meta.get("title", "Meeting")
        c.execute("DELETE FROM chunks WHERE mid=?", (mid,))
        c.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                  (mid, title, "title", "", 0, title))
        tf = mdir / "transcript.json"
        if tf.exists():
            for seg in json.loads(tf.read_text(encoding="utf-8")):
                c.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                          (mid, title, "utterance", seg.get("speaker", ""),
                           seg.get("start_ms", 0), seg.get("text", "")))
        nf = mdir / "note.md"
        if nf.exists():
            for para in nf.read_text(encoding="utf-8").split("\n\n"):
                if para.strip():
                    c.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                              (mid, title, "note", "", 0, para.strip()[:2000]))
        c.execute("INSERT OR REPLACE INTO indexed VALUES (?,?)", (mid, stamp))
    for gone in set(known) - seen:  # deleted meetings
        c.execute("DELETE FROM chunks WHERE mid=?", (gone,))
        c.execute("DELETE FROM indexed WHERE mid=?", (gone,))
    c.commit()
    c.close()


_STOP = {"a", "an", "and", "are", "about", "at", "be", "by", "can", "class",
         "could", "did", "do", "does", "down", "for", "from", "had", "has",
         "have", "he", "her", "his", "how", "i", "in", "is", "it", "its",
         "me", "meeting", "my", "of", "on", "or", "our", "say", "said",
         "she", "so", "teach", "that", "the", "their", "them", "then",
         "there", "they", "this", "to", "up", "us", "was", "we", "were",
         "what", "when", "where", "which", "who", "why", "will", "with",
         "would", "you", "your"}


def _fts_query(q: str) -> str:
    """Quote terms so punctuation can't break FTS5 syntax; OR the content
    words. Stopwords are dropped (a question's 'what did the…' otherwise
    drowns out its rare, high-signal terms in bm25)."""
    words = [w.strip('.,?!:;()"\'').replace('"', "") for w in q.split()]
    keep = [w for w in words if w and w.lower() not in _STOP]
    if not keep:  # query was all stopwords — fall back to everything
        keep = [w for w in words if w]
    return " OR ".join(f'"{w}"' for w in keep) if keep else '""'


NOTE_BONUS = 1.5     # bm25 is negative; subtracting ranks notes higher
MIN_UTTERANCE = 60   # chars — shorter matches are noise, not evidence
SNIPPET_TOKENS = 60  # FTS5 caps snippet() at 64
NOTE_WINDOW = (250, 750)  # chars kept before/after a match inside a note


def _window(text: str, terms: list[str]) -> str:
    """A generous window around the first matching term.

    The old 18-token snippet was the real reason Ask answered wrongly from
    correct hits: a note chunk would match on 'personality types' and hand the
    model the summary line, while the actual answer sat 600 characters further
    down the same chunk. Retrieval was never the whole problem — what got
    passed on was."""
    low = text.lower()
    i = min((low.find(t.lower()) for t in terms if t and low.find(t.lower()) >= 0),
            default=-1)
    if i < 0:
        return text[:sum(NOTE_WINDOW)]
    before, after = NOTE_WINDOW
    start, end = max(0, i - before), min(len(text), i + after)
    return ("…" if start else "") + text[start:end].strip() + \
           ("…" if end < len(text) else "")


def search(query: str, limit: int = 12) -> list[dict]:
    refresh()
    terms = [w.strip('.,?!:;()"\'') for w in query.split()
             if w.strip('.,?!:;()"\'').lower() not in _STOP]
    # Query the two kinds SEPARATELY. Ranking them together is useless: a
    # transcript has thousands of short utterances to a handful of note
    # sections, so notes never even reached the fetch window and any bonus
    # applied to them was academic.
    sql = f"""SELECT mid, title, kind, speaker, ts_ms,
                     snippet(chunks, 5, '[', ']', '…', {SNIPPET_TOKENS}),
                     bm25(chunks), text
              FROM chunks WHERE chunks MATCH ? AND kind {{}} 'note'
              ORDER BY bm25(chunks) LIMIT ?"""
    c = _conn()
    fts = _fts_query(query)
    rows = c.execute(sql.format("="), (fts, max(limit, 12))).fetchall() + \
        c.execute(sql.format("!="), (fts, max(limit * 6, 40))).fetchall()
    c.close()

    scored = []
    for mid, title, kind, speaker, ts, snip, score, text in rows:
        if kind != "note" and len(text or "") < MIN_UTTERANCE:
            continue  # "I taught him." outranking a whole teaching section
        scored.append((score,
                       {"meeting_id": mid, "meeting_title": title, "kind": kind,
                        "speaker": speaker, "ts_ms": ts,
                        "snippet": _window(text, terms) if kind == "note" else snip}))
    scored.sort(key=lambda x: x[0])

    # Reserve slots for note chunks. A rank bonus alone never works: a question
    # like "what is emotional contagion" appears verbatim in a dozen short
    # utterances, and bm25 always prefers short documents, so the AI note —
    # the one place the answer is stated plainly — got crowded out every time.
    notes = [h for _, h in scored if h["kind"] == "note"]
    rest = [h for _, h in scored if h["kind"] != "note"]

    # Let the utterance hits vote on which meeting matters. A question like
    # "what is emotional contagion" matches the phrase in many meetings, but
    # the section that DEFINES it sits in the notes of the meeting the
    # transcript hits already agree on — globally it ranked 9th.
    voted = {h["meeting_id"] for h in rest[:5]}
    notes.sort(key=lambda h: h["meeting_id"] not in voted)  # stable
    keep_notes = notes[:max(1, limit // 2)]

    # ...and cap one meeting dominating the utterance slots, so a question
    # spanning several meetings still sees all of them
    per_meeting, picked = {}, []
    for h in rest:
        n = per_meeting.get(h["meeting_id"], 0)
        if n >= 3:
            continue
        per_meeting[h["meeting_id"]] = n + 1
        picked.append(h)

    out = keep_notes + picked[:max(0, limit - len(keep_notes))]
    return out[:limit]
