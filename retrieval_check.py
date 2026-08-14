"""Measure retrieval alone: does the context Ask hands the model actually
contain the answer?

Separating this from ask_reliability.py matters — a wrong answer can come from
bad retrieval or from the model ignoring good context, and the fixes are
completely different. This runs no LLM, so it is fast and cheap enough to run
while a recording is in progress.

Reuses the grounded questions in data/ask_eval.json.
Usage: .venv/bin/python retrieval_check.py
"""
import json
import sys
from pathlib import Path

import meeting_tools

APP_DIR = Path(__file__).resolve().parent
EVAL_FILE = APP_DIR / "data" / "ask_eval.json"


def main() -> int:
    if not EVAL_FILE.exists():
        print(f"no question set at {EVAL_FILE}")
        return 2
    cases = [c for c in json.loads(EVAL_FILE.read_text(encoding="utf-8"))
             if c.get("kind") != "honest"]
    ok = 0
    for c in cases:
        hits = meeting_tools.search_meetings(c["q"], limit=8)
        # the model is shown the meeting title alongside each snippet, so it
        # counts as retrieved context (matters for "which classes did I record")
        blob = " ".join(f"{h.get('meeting_title','')} {h.get('snippet','')}"
                        for h in hits).lower()
        missing = [grp[0] for grp in c.get("expect", [])
                   if not any(s.lower() in blob for s in grp)]
        good = not missing
        ok += good
        print(f"  {'HIT ' if good else 'MISS'} {c['id']:<22} "
              f"{len(hits)} hits, {len(blob)} chars"
              + ("" if good else f" | absent: {', '.join(missing[:3])}"))
    print(f"\nretrieval recall: {ok}/{len(cases)} "
          f"({100*ok/len(cases):.0f}%) — the answer was present in the "
          f"context handed to the model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
