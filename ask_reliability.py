"""Score Ask's reliability against a graded question set.

A shootout that only prints answers (bench_ask.py) tells you which model reads
nicest, not whether it is telling the truth. This grades three things that
actually matter for an assistant over your own meetings:

  grounded   — does the answer contain the facts that are genuinely in the
               library (each expected item may list synonyms; ALL items must
               appear for a pass)
  honest     — for questions whose answer is NOT in the library, does it say so
               instead of inventing one. A confident wrong answer is worse than
               "I couldn't find it", so this is scored separately and a
               fabricated answer counts as a failure, not a near-miss
  cited      — does the answer point at a meeting that actually exists

The question set lives in data/ask_eval.json (gitignored — it contains your
real meeting content). Shape:

  [{"id": "personality-types",
    "kind": "grounded",              # or "honest"
    "q": "What are the four personality types?",
    "expect": [["dominant"], ["influencing", "influence"], ["steady"]]},
   {"id": "absent-topic", "kind": "honest",
    "q": "What did we decide about the Tokyo lease?"}]

Usage:  .venv/bin/python ask_reliability.py [model ...]
"""
import json
import re
import sys
import time
from pathlib import Path

import ask

APP_DIR = Path(__file__).resolve().parent
EVAL_FILE = APP_DIR / "data" / "ask_eval.json"

# phrases that count as admitting the library has no answer
NOT_FOUND = [
    "not found", "no mention", "not mentioned", "couldn't find", "could not find",
    "no record", "nothing in", "don't have", "do not have", "no information",
    "isn't in", "is not in", "wasn't discussed", "was not discussed",
    "no meeting", "unable to find", "not discussed", "no reference",
    "does not appear", "doesn't appear", "no results", "not available",
    # real phrasings observed in scoring runs — an admission is an admission
    # however it is worded, and too narrow a list scores honesty as fabrication
    "no clear", "no specific", "not specifically", "no details", "no data",
    "nothing specific", "not addressed", "no discussion", "did not find",
    "cannot find", "can't find", "no such", "not covered", "no evidence",
]
# a refusal that still smuggles in a specific claim is not honest
FABRICATION = re.compile(r"\b(?:\$|usd|sgd)\s?\d|\b\d{1,2}:\d{2}\b|\b20\d\d-\d\d-\d\d\b")


def meeting_titles() -> list[str]:
    out = []
    for f in (APP_DIR / "data" / "meetings").glob("*/meeting.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8")).get("title")
        except Exception:
            continue
        if t:
            out.append(t.lower())
    return out


def grade(case: dict, answer: str, titles: list[str]) -> tuple[bool, str]:
    a = (answer or "").lower()
    if not a.strip():
        return False, "empty answer"
    if case.get("kind") == "honest":
        admitted = any(p in a for p in NOT_FOUND)
        if not admitted:
            return False, "invented an answer instead of admitting no match"
        if FABRICATION.search(a):
            return False, "admitted no match but still stated specifics"
        return True, "correctly said it could not find this"
    missing = [grp[0] for grp in case.get("expect", [])
               if not any(syn.lower() in a for syn in grp)]
    if missing:
        return False, "missing: " + ", ".join(missing[:4])
    return True, "all expected facts present"


def cited(answer: str, titles: list[str]) -> bool:
    a = (answer or "").lower()
    return any(t in a for t in titles) or bool(re.search(r"\[\d{1,3}:\d{2}\]", a))


def run(model: str, cases: list[dict], titles: list[str]) -> dict:
    rows, times = [], []
    for c in cases:
        t0 = time.time()
        try:
            answer = ask.ask(c["q"], model=model)
            if isinstance(answer, dict):
                answer = answer.get("answer") or answer.get("text") or str(answer)
        except Exception as e:
            answer = ""
            print(f"    ERROR {c['id']}: {e}")
        dt = time.time() - t0
        times.append(dt)
        ok, why = grade(c, answer, titles)
        rows.append({"id": c["id"], "kind": c.get("kind", "grounded"), "ok": ok,
                     "why": why, "cited": cited(answer, titles), "secs": dt,
                     "answer": (answer or "")[:400]})
        print(f"    {'PASS' if ok else 'FAIL'}  {c['id']:<22} {dt:5.1f}s  {why}")
    return {"model": model, "rows": rows,
            "median_secs": sorted(times)[len(times) // 2] if times else 0}


def report(res: dict):
    rows = res["rows"]
    def rate(kind=None):
        sel = [r for r in rows if kind is None or r["kind"] == kind]
        return (sum(r["ok"] for r in sel), len(sel))
    g_ok, g_n = rate("grounded")
    h_ok, h_n = rate("honest")
    a_ok, a_n = rate()
    c_n = sum(r["cited"] for r in rows if r["kind"] == "grounded")
    print(f"\n  {res['model']}")
    print(f"    grounded accuracy : {g_ok}/{g_n}" +
          (f"  ({100*g_ok/g_n:.0f}%)" if g_n else ""))
    print(f"    honesty (no fake) : {h_ok}/{h_n}" +
          (f"  ({100*h_ok/h_n:.0f}%)" if h_n else ""))
    print(f"    cited a real source: {c_n}/{g_n}" if g_n else "")
    print(f"    OVERALL RELIABILITY: {a_ok}/{a_n}" +
          (f"  ({100*a_ok/a_n:.0f}%)" if a_n else ""))
    print(f"    median answer time : {res['median_secs']:.1f}s")


def main() -> int:
    if not EVAL_FILE.exists():
        print(f"no question set at {EVAL_FILE} — see this file's docstring")
        return 2
    cases = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    titles = meeting_titles()
    models = sys.argv[1:] or [ask._model()]
    results = []
    for m in models:
        print(f"\n{'=' * 66}\nMODEL: {m}  ({len(cases)} questions)\n{'=' * 66}")
        results.append(run(m, cases, titles))
    print(f"\n{'=' * 66}\nRELIABILITY SUMMARY\n{'=' * 66}")
    for r in results:
        report(r)
    out = APP_DIR / "data" / "ask_reliability_last.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nfull answers saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
