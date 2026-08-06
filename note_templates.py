"""AI Note templates — Fellow's model: a template is an ordered list of
sections, each with an extraction prompt. Built-ins below; custom templates
can be added in data/templates.json (same shape, "builtin" omitted)."""
import json
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CUSTOM_FILE = APP_DIR / "data" / "templates.json"

BUILTINS = [
    {"id": "general", "name": "General Meeting", "builtin": True,
     "has_actions": True, "has_decisions": True,
     "sections": [
         {"title": "Topics", "dynamic": True,
          "prompt": "Segment the meeting chronologically into topics, each "
                    "with substantive bullets capturing every distinct point, "
                    "example and reference."}]},

    {"id": "minutes", "name": "Formal Minutes", "builtin": True,
     "has_actions": True, "has_decisions": True,
     "sections": [
         {"title": "Attendees",
          "prompt": "People present, inferred from speaker names and explicit "
                    "mentions. One bullet per person, with role if stated. "
                    "Note that unnamed speakers exist if any."},
         {"title": "Agenda Items Discussed",
          "prompt": "The distinct matters discussed, in order. One bullet per "
                    "item with a neutral 1-2 sentence account of the "
                    "discussion. Formal third-person register."},
         {"title": "Matters Arising",
          "prompt": "Issues raised for future attention that are neither "
                    "decisions nor assigned action items. Empty if none."},
         {"title": "Next Meeting",
          "prompt": "Any mention of the next meeting's date, time, venue, or "
                    "planned topics. Empty if none."}]},

    {"id": "lecture", "name": "Lecture / Class", "builtin": True,
     "has_actions": False, "has_decisions": False,
     "sections": [
         {"title": "Key Teachings",
          "prompt": "The main teaching points, comprehensive, each with its "
                    "cited references (scripture book chapter:verse, book "
                    "titles, names) exactly as cited."},
         {"title": "Illustrations & Stories",
          "prompt": "Anecdotes, testimonies, and examples used, each with the "
                    "point it illustrated."},
         {"title": "Practical Applications",
          "prompt": "What listeners were urged to do, practice, or apply."},
         {"title": "Q&A",
          "prompt": "Questions asked by attendees and the answers given. "
                    "Empty if none."}]},

    {"id": "discovery", "name": "Client Discovery", "builtin": True,
     "has_actions": True, "has_decisions": False,
     "sections": [
         {"title": "Meeting Outcome",
          "prompt": "The impact of this meeting on the deal moving forward, "
                    "in 1-2 concise sentences."},
         {"title": "Sentiment",
          "prompt": "The client's sentiment (positive/neutral/negative) with "
                    "a one-line reasoning."},
         {"title": "Customer Needs",
          "prompt": "Problems, needs, and pain points the client expressed."},
         {"title": "Decision Maker",
          "prompt": "Who has authority to make the purchase decision; who "
                    "influences it."},
         {"title": "Budget",
          "prompt": "Anything the client said about budget."},
         {"title": "Competitors",
          "prompt": "Competitors or alternative solutions mentioned."},
         {"title": "Objections",
          "prompt": "Concerns or objections raised, and how they were "
                    "addressed."}]},
]


def all_templates() -> list[dict]:
    out = list(BUILTINS)
    if CUSTOM_FILE.exists():
        try:
            for t in json.loads(CUSTOM_FILE.read_text(encoding="utf-8")):
                if t.get("id") and t.get("sections"):
                    out.append(t)
        except Exception:
            pass
    return out


def by_id(tid: str) -> dict:
    for t in all_templates():
        if t["id"] == tid:
            return t
    return BUILTINS[0]
