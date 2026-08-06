"""Ask-model shootout: run the same grounded questions through each candidate
model over the REAL meeting library and print answers + timings for grading.
Usage: .venv/bin/python bench_ask.py [model ...]"""
import sys
import time

import ask

# Adapt these to YOUR library: mix factual questions with known answers,
# a question whose answer is NOT in the library (honesty test), a
# non-English question, and a cross-meeting aggregation.
QUESTIONS = [
    ("facts",
     "What was decided about the project launch date, and who will update the plan?"),
    ("content",
     "What were the main points of the most recent lecture or presentation?"),
    ("honesty",
     "What was said about a topic that was never actually discussed?"),
    ("chinese",
     "会议里关于产品发布日期有什么决定？请用中文回答。"),
    ("cross-meeting",
     "List every action item that was assigned to anyone across all my meetings this week."),
]

MODELS = sys.argv[1:] or ["qwen2.5:7b-instruct", "hermes3:8b", "gemma4:12b"]

for model in MODELS:
    print(f"\n{'=' * 70}\nMODEL: {model}\n{'=' * 70}")
    for key, q in QUESTIONS:
        t0 = time.time()
        try:
            r = ask.ask(q, model=model)
            secs = time.time() - t0
            tools = ",".join(s["tool"] for s in r["steps"]) or "-"
            print(f"\n--- [{key}] {secs:.0f}s tools={tools}")
            print(r["answer"][:650])
        except Exception as e:
            print(f"\n--- [{key}] FAILED: {e}")
