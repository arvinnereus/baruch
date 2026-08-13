"""Local Ask agent: tool-calling loop over the meeting library via Ollama.
Model configurable in settings.json ("ask_model"); zero API tokens."""
import json
import urllib.request
from pathlib import Path

import meeting_tools

APP_DIR = Path(__file__).resolve().parent
OLLAMA = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"
MAX_TURNS = 6

TOOLS = [
    {"type": "function", "function": {
        "name": "search_meetings",
        "description": "Full-text search across ALL meeting transcripts, AI notes and titles. Use this FIRST for any question about what was said or discussed.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "2-5 keywords"},
            "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "list_meetings",
        "description": "List recent meetings with ids, titles, dates, durations.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_transcript",
        "description": "Read the speaker-labelled transcript of one meeting. Optionally pass start_ts/end_ts (MM:SS) to read a window around a search hit.",
        "parameters": {"type": "object", "properties": {
            "meeting_id": {"type": "string"},
            "start_ts": {"type": "string"}, "end_ts": {"type": "string"}},
            "required": ["meeting_id"]}}},
    {"type": "function", "function": {
        "name": "get_note",
        "description": "Read the AI note (summary, action items, topics) of one meeting.",
        "parameters": {"type": "object", "properties": {
            "meeting_id": {"type": "string"}}, "required": ["meeting_id"]}}},
]

FUNCS = {"search_meetings": meeting_tools.search_meetings,
         "list_meetings": meeting_tools.list_meetings,
         "get_transcript": meeting_tools.get_transcript,
         "get_note": meeting_tools.get_note}

SYSTEM = """You are Ask Baruch, an assistant with tool access to the user's private library of meeting recordings (transcripts + AI notes), all stored locally. Every question is about what was said in these meetings — including lectures, classes, and sermons the user recorded.
Rules:
- NEVER refuse and NEVER ask permission. Search results for the question are provided in a CONTEXT block; use get_transcript (with the meeting_id from a hit and start_ts near it) or get_note to read more before answering, or call search_meetings again with different keywords.
- Your reply must be a SYNTHESIZED answer in your own words. NEVER reproduce the CONTEXT block, raw JSON, or tool output verbatim — the user cannot see them and they are not an answer.
- Ground every claim in tool results. Cite the meeting title and [MM:SS] timestamps for key statements.
- Before claiming that a person did NOT say something, you MUST read their actual words with get_transcript first — absence from snippets is not evidence.
- If nothing relevant is found after searching, say so plainly.
- Be concise and direct. Answer in the language of the user's question."""


def _fmt_hits(hits: list[dict]) -> str:
    """Compact, readable one-line-per-hit rendering (avoids JSON echo)."""
    if not hits:
        return "(no matches)"
    lines = []
    for h in hits:
        who = f" {h['speaker']}:" if h.get("speaker") else ""
        lines.append(f"• {h['meeting_title']} (id {h['meeting_id']}) "
                     f"[{h.get('ts','00:00')}]{who} {h.get('snippet','')}")
    return "\n".join(lines)


_ECHO_MARKERS = ("[Automatic search", "CONTEXT (search hits", '{"meeting_id"',
                 "[{'meeting_id'")


def _sanitize(answer: str) -> str:
    """Strip any echoed context/tool blocks that leaked into the answer."""
    for marker in _ECHO_MARKERS:
        idx = answer.find(marker)
        if idx != -1:
            answer = answer[:idx]
    return answer.strip()


def _model() -> str:
    f = APP_DIR / "data" / "settings.json"
    if f.exists():
        return json.loads(f.read_text()).get("ask_model") or DEFAULT_MODEL
    return DEFAULT_MODEL


def _chat(messages, model, with_tools=True):
    payload = {"model": model, "stream": False,
               "options": {"temperature": 0.2, "num_ctx": 16384},
               "messages": messages}
    if with_tools:
        payload["tools"] = TOOLS
    body = json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["message"]


def ask(question: str, history: list[dict] | None = None,
        model: str | None = None) -> dict:
    """Run the agent loop. Returns {answer, steps, model}.

    The first search is forced (RAG seeding): small local models sometimes
    deflect instead of calling tools, so we search the question terms up
    front and hand the model grounded hits to start from."""
    model = model or _model()
    seed = meeting_tools.search_meetings(question, limit=8)
    # cross-language retrieval: transcripts are mostly English, so a Chinese
    # question needs an English-keyword seed as well
    if any("一" <= c <= "鿿" for c in question):
        try:
            kw = _chat([{"role": "system", "content":
                         "Translate the user's question into 3-6 short English "
                         "search keywords. Reply with ONLY the keywords."},
                        {"role": "user", "content": question}], model,
                       with_tools=False)
            extra = meeting_tools.search_meetings(kw.get("content", ""), limit=8)
            seen = {(h["meeting_id"], h["ts"], h["snippet"]) for h in seed}
            seed += [h for h in extra
                     if (h["meeting_id"], h["ts"], h["snippet"]) not in seen]
        except Exception:
            pass
    user_msg = (f"{question}\n\n"
                f"CONTEXT (search hits — internal, never repeat verbatim; "
                f"verify with get_transcript/get_note, then answer in your "
                f"own words):\n{_fmt_hits(seed)}")
    messages = [{"role": "system", "content": SYSTEM}] + (history or []) + \
        [{"role": "user", "content": user_msg}]
    steps = [{"tool": "search_meetings", "args": {"query": question[:60]}}]
    for _ in range(MAX_TURNS):
        try:
            msg = _chat(messages, model)
        except Exception as e:
            return {"answer": f"Ollama error: {e}", "steps": steps, "model": model}
        calls = msg.get("tool_calls") or []
        if not calls:
            answer = _sanitize(msg.get("content", ""))
            if not answer:  # everything was echoed junk — demand a real answer
                messages.append(msg)
                messages.append({"role": "user", "content":
                                 "That was raw context, not an answer. Reply "
                                 "with a concise synthesized answer only."})
                try:
                    msg = _chat(messages, model, with_tools=False)
                    answer = _sanitize(msg.get("content", ""))
                except Exception:
                    pass
            return {"answer": answer or "(no answer)", "steps": steps,
                    "model": model}
        messages.append(msg)
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            try:
                result = FUNCS[name](**args) if name in FUNCS \
                    else f"unknown tool {name}"
            except Exception as e:
                result = f"tool error: {e}"
            if name == "search_meetings" and isinstance(result, list):
                result = _fmt_hits(result)
            steps.append({"tool": name, "args": args})
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(result, ensure_ascii=False)
                             if not isinstance(result, str) else result})
    return {"answer": "(stopped after too many tool calls — try a narrower question)",
            "steps": steps, "model": model}
