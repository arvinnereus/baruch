"""Score AI-note quality for a model, against real meetings.

"Note quality" sounds subjective, but every note failure this project has
actually suffered was objectively checkable, and those are what this measures:

  grounded_names  a note once credited a class to "Chin", a person never in the
                  meeting — prompt example names had leaked into the output.
                  Every capitalised name in the note must appear in the
                  transcript.
  valid_ts        timestamps must fall inside the recording. A note citing
                  (58:14) on a 40-minute meeting is fabricated structure.
  ts_accuracy     a sampled bullet's timestamp should land near text that
                  shares wording with the bullet — a plausible timestamp on
                  the wrong moment is worse than none.
  coverage        share of the meeting's distinctive vocabulary that survives
                  into the note. Catches a note that is fluent but empty.
  no_repeats      duplicate bullets (map-reduce over chunks can restate).
  sections        every section the template asks for is present and non-empty.
  bullets/chars   volume, for context — not scored, since longer is not better.

It never touches the stored note: generation happens in a temp directory and
the meeting on disk is left exactly as it was.

  .venv/bin/python note_quality.py <model> [<model2> ...] [--meetings N]
"""
import json
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pipeline

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data" / "meetings"

STOP = {"the", "and", "that", "this", "with", "have", "from", "they", "what",
        "when", "will", "your", "you", "for", "are", "was", "but", "not",
        "his", "her", "she", "him", "our", "who", "them", "then", "than",
        "there", "their", "would", "could", "should", "about", "into",
        "because", "which", "these", "those", "been", "were", "also", "just",
        "like", "know", "going", "want", "said", "say", "one", "two", "get",
        "got", "can", "very", "really", "okay", "yeah", "right", "thing",
        "things", "people", "time", "way", "make", "made", "come", "came"}


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOP]


def note_text(note: dict) -> str:
    """All prose in a note, whatever template shape it has."""
    out = [note.get("summary", "")]
    for key in ("action_items", "decisions"):
        for it in note.get(key) or []:
            out.append(it.get("text", "") if isinstance(it, dict) else str(it))
    for t in note.get("topics") or []:
        out.append(t.get("title", ""))
        out += [b.get("text", "") if isinstance(b, dict) else str(b)
                for b in (t.get("bullets") or [])]
    for s in note.get("sections") or []:
        out.append(s.get("title", ""))
        out += [b.get("text", "") if isinstance(b, dict) else str(b)
                for b in (s.get("bullets") or [])]
    return "\n".join(x for x in out if x)


def bullet_items(note: dict) -> list[dict]:
    """Bullets with their timestamp. The timestamp is a separate `ts` field,
    not inline in the text — scanning the text for "(MM:SS)" found nothing and
    reported timestamp accuracy as unmeasurable."""
    out = []
    for t in (note.get("topics") or []) + (note.get("sections") or []):
        for b in t.get("bullets") or []:
            out.append(b if isinstance(b, dict) else {"text": str(b)})
    for key in ("action_items", "decisions"):
        for i in note.get(key) or []:
            out.append(i if isinstance(i, dict) else {"text": str(i)})
    return [b for b in out if b.get("text")]


def bullets(note: dict) -> list[str]:
    return [b["text"] for b in bullet_items(note)]


def section_titles(note: dict) -> set[str]:
    """Template-supplied headings ("Key Teachings", "Attendees") are not the
    model's words and must not count as invented names."""
    out = set()
    for t in (note.get("topics") or []) + (note.get("sections") or []):
        out |= {w.lower() for w in re.findall(r"[A-Za-z]{2,}", t.get("title", ""))}
    return out


def timestamps(note: dict) -> list[int]:
    secs = []
    for m in re.finditer(r"\((\d{1,3}):(\d{2})\)", json.dumps(note)):
        secs.append(int(m.group(1)) * 60 + int(m.group(2)))
    return secs


def score(note: dict, segs: list[dict], duration_s: int) -> dict:
    text = note_text(note)
    tr_text = " ".join(s.get("text", "") for s in segs)
    tr_words = set(words(tr_text))

    # Names the model invented.
    # Compared against EVERY word in the transcript, not only capitalised ones:
    # a word the note capitalises ("Sealed by blood") is usually lowercase in
    # speech, and matching only capitals reported it as invented.
    allowed = {w.lower() for w in re.findall(r"[A-Za-z]{2,}", tr_text)}
    for s in {s.get("speaker", "") for s in segs}:
        allowed |= {w.lower() for w in re.findall(r"[A-Za-z]{2,}", s or "")}
    # Only MID-SENTENCE capitals: a word opening a sentence or bullet is
    # capitalised by grammar, not because it is a name ("Adapt your style…").
    # The real defect looked like "led by Chin" — mid-sentence — so this keeps
    # what matters and drops the noise.
    # [ \t]+ not \s+ : bullets are joined by newlines, so \s treated every
    # bullet's first word as mid-sentence and flagged ordinary verbs
    cited_names = {m.group(1).lower() for m in
                   re.finditer(r"[a-z,][ \t]+([A-Z][a-z]{2,})\b", text)}
    invented = sorted(n for n in cited_names - allowed - section_titles(note)
                      if n not in STOP)[:6]

    ts = timestamps(note) + [int(m.group(1))*60+int(m.group(2))
          for b in bullet_items(note)
          for m in [re.match(r"(\d{1,3}):(\d{2})", str(b.get("ts") or ""))] if m]
    bad_ts = [t for t in ts if duration_s and t > duration_s + 60]

    # does a sampled bullet's timestamp land near matching words?
    near = tot = 0
    for b in bullet_items(note)[:12]:
        m = re.match(r"(\d{1,3}):(\d{2})", str(b.get("ts") or "")) or \
            re.search(r"\((\d{1,3}):(\d{2})\)", b["text"])
        if not m:
            continue
        at = (int(m.group(1)) * 60 + int(m.group(2))) * 1000
        window = " ".join(s.get("text", "") for s in segs
                          if abs(s.get("start_ms", 0) - at) < 120000).lower()
        bw = set(words(b["text"]))
        if bw:
            tot += 1
            near += len(bw & set(words(window))) / len(bw) >= 0.25

    nb = bullets(note)
    dupes = len(nb) - len(set(b.strip().lower() for b in nb))

    tpl_sections = [s.get("title") for s in (note.get("sections") or [])] or \
                   [t.get("title") for t in (note.get("topics") or [])]
    empty_sections = sum(1 for s in (note.get("sections") or [])
                         if not (s.get("bullets") or []))

    nw = set(words(text))
    coverage = len(nw & tr_words) / max(1, len(tr_words))

    return {
        "invented_names": invented,
        "bad_timestamps": len(bad_ts),
        "ts_accuracy": (near / tot) if tot else None,
        "coverage": coverage,
        "duplicate_bullets": dupes,
        "sections": len(tpl_sections),
        "empty_sections": empty_sections,
        "bullets": len(nb),
        "chars": len(text),
    }


def pick_meetings(n: int) -> list[Path]:
    rows = []
    for f in DATA.glob("*/meeting.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        t = f.parent / "transcript.json"
        if str(m.get("status", "")).startswith("ready") and t.exists():
            rows.append((m.get("duration_s") or 0, f.parent, m))
    rows.sort(reverse=True)          # longest first: hardest for map-reduce
    return [(d, m) for _, d, m in rows[:n]]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = 2
    if "--meetings" in sys.argv:
        n = int(sys.argv[sys.argv.index("--meetings") + 1])
    models = args or [pipeline.ollama_model()]
    targets = pick_meetings(n)
    if not targets:
        print("no ready meetings with transcripts")
        return 2

    results = {m: [] for m in models}
    for d, meta in targets:
        segs = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
        if isinstance(segs, dict):
            segs = segs.get("segments", segs.get("lines", []))
        dur = meta.get("duration_s") or 0
        print(f"\n=== {meta.get('title','?')[:40]} "
              f"({dur//60} min, {len(segs)} segments, "
              f"template={meta.get('template_id','general')}) ===", flush=True)
        for model in models:
            tmp = Path(tempfile.mkdtemp(prefix="notebench-"))
            t0 = time.time()
            try:
                note = pipeline.generate_note(
                    segs, meta.get("context", ""), tmp,
                    meta.get("template_id", "general"), model=model)
            except Exception as e:
                print(f"  {model}: FAILED {e}", flush=True)
                shutil.rmtree(tmp, ignore_errors=True)
                continue
            dt = time.time() - t0
            shutil.rmtree(tmp, ignore_errors=True)
            if note.get("error"):
                print(f"  {model}: error {note['error']}", flush=True)
                continue
            s = score(note, segs, dur)
            s["secs"] = dt
            results[model].append(s)
            acc = "n/a" if s["ts_accuracy"] is None else f"{s['ts_accuracy']*100:.0f}%"
            print(f"  {model:20s} {dt:6.0f}s  bullets {s['bullets']:3d}  "
                  f"coverage {s['coverage']*100:4.1f}%  ts-acc {acc:>4}  "
                  f"bad-ts {s['bad_timestamps']}  dupes {s['duplicate_bullets']}  "
                  f"invented {s['invented_names'] or 'none'}", flush=True)

    print(f"\n{'='*70}\nNOTE QUALITY SUMMARY ({len(targets)} meetings)\n{'='*70}")
    for model, rows in results.items():
        if not rows:
            print(f"  {model}: no results")
            continue
        avg = lambda k: statistics.mean(r[k] for r in rows if r[k] is not None) \
            if any(r[k] is not None for r in rows) else 0
        inv = sum(len(r["invented_names"]) for r in rows)
        print(f"\n  {model}")
        print(f"    coverage of transcript vocabulary : {avg('coverage')*100:.1f}%")
        print(f"    timestamp accuracy                : {avg('ts_accuracy')*100:.0f}%")
        print(f"    invented names (0 is required)    : {inv}")
        print(f"    timestamps outside the recording  : {sum(r['bad_timestamps'] for r in rows)}")
        print(f"    duplicate bullets                 : {sum(r['duplicate_bullets'] for r in rows)}")
        print(f"    empty sections                    : {sum(r['empty_sections'] for r in rows)}")
        print(f"    bullets per note                  : {avg('bullets'):.0f}")
        print(f"    time per note                     : {avg('secs'):.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
