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


def search(query: str, limit: int = 12) -> list[dict]:
    refresh()
    c = _conn()
    rows = c.execute(
        """SELECT mid, title, kind, speaker, ts_ms,
                  snippet(chunks, 5, '[', ']', '…', 18), bm25(chunks)
           FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks) LIMIT ?""",
        (_fts_query(query), limit)).fetchall()
    c.close()
    return [{"meeting_id": r[0], "meeting_title": r[1], "kind": r[2],
             "speaker": r[3], "ts_ms": r[4], "snippet": r[5]} for r in rows]
