"""Baruch pipeline: audio tracks -> transcript -> AI Note.

Calibration (2026-07-28, poc/calibrate.sh): feed whisper RAW audio (no filters),
beam size 8, optional domain-vocabulary prompt. speechnorm/denoise cause
hallucination loops on quiet audio.
"""
import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# whisper models are searched in app/models first, then any sibling install
MODEL_SEARCH = [APP_DIR / "models", Path.home() / "ClearCaption/models"]
OLLAMA = "http://127.0.0.1:11434"
LLM_PREFERRED = ["qwen2.5:7b-instruct", "qwen2.5:7b", "qwen2.5:1.5b"]


def whisper_model() -> Path:
    """Prefer full large-v3 (best accuracy on hard/far-field audio) over turbo."""
    for name in ("ggml-large-v3.bin", "ggml-large-v3-turbo.bin"):
        for mdir in MODEL_SEARCH:
            p = mdir / name
            if p.exists() and p.stat().st_size > 1 << 30:  # guard: partial dl
                return p
    return APP_DIR / "models" / "ggml-large-v3-turbo.bin"


def log(mdir: Path, msg: str):
    with open(mdir / "pipeline.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def to_wav(src: Path, dst: Path, rate: int = 48000):
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-ar", str(rate), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    if dst.exists() and dst.stat().st_size > 44:
        return True
    # voicemic CAFs are 9-channel (voice-processing unit: mic + reference
    # channels) — ffmpeg cannot auto-downmix that layout and writes NOTHING.
    # Channel 0 is the processed voice: take it explicitly.
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-af", "pan=mono|c0=c0", "-ar", str(rate),
         "-c:a", "pcm_s16le", str(dst)])
    return dst.exists() and dst.stat().st_size > 44


def raw_to_wav(src: Path, dst: Path, in_rate: int = 48000):
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", str(in_rate), "-ac", "1", "-i", str(src),
         "-c:a", "pcm_s16le", str(dst)])
    return dst.exists() and dst.stat().st_size > 44


def mean_volume_db(wav: Path) -> float:
    r = run(["ffmpeg", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else 0.0


def whisper_input(wav: Path) -> Path:
    """16 kHz mono copy for whisper, with a STATIC gain boost when the source is
    quiet (far-field lecture). Static gain only — dynamic normalizers caused
    hallucination loops in calibration."""
    dst = wav.with_name(wav.stem + ".16k.wav")
    mv = mean_volume_db(wav)
    af = ["aresample=resampler=soxr" if "soxr" in
          run(["ffmpeg", "-hide_banner", "-buildconf"]).stdout else "aresample=16000"]
    if mv < -28:
        gain = min(20.0, -22.0 - mv)
        af.append(f"volume={gain:.1f}dB")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
         "-af", ",".join(af), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    return dst if dst.exists() else wav


def drop_silent_segments(segs: list[dict], wav16: Path) -> list[dict]:
    """Whisper hallucinates phrases ('Thank you.') over near-silence. Drop
    segments whose actual audio energy is below speech level."""
    try:
        import wave
        import numpy as np
        with wave.open(str(wav16)) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()),
                                  dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return segs
    kept = []
    for s in segs:
        clip = audio[s["start_ms"] * 16: s["end_ms"] * 16]
        if not len(clip):
            continue
        rms = float(np.sqrt(np.mean(clip ** 2)))
        db = 20 * np.log10(rms + 1e-9)
        if db >= -48:
            kept.append(s)
    return kept


def _whisper_pass(src: Path, out: Path, prompt: str,
                  extra: list[str] | None = None) -> list[dict]:
    cmd = ["whisper-cli", "-m", str(whisper_model()), "-f", str(src),
           "-bs", "8", "-bo", "8", "-oj", "-of", str(out)]
    if extra:
        cmd += extra
    if prompt.strip():
        cmd += ["--prompt", prompt.strip()]
    run(cmd)
    jf = Path(str(out) + ".json")
    if not jf.exists():
        return []
    data = json.loads(jf.read_text(encoding="utf-8"))
    segs = []
    for s in data.get("transcription", []):
        text = s.get("text", "").strip()
        if not text:
            continue
        segs.append({"start_ms": s["offsets"]["from"],
                     "end_ms": s["offsets"]["to"], "text": text})
    return segs


def _uniq_ratio(segs: list[dict]) -> float:
    if not segs:
        return 1.0
    texts = [s["text"] for s in segs]
    return len(set(texts)) / len(texts)


def transcribe(wav: Path, prompt: str = "") -> list[dict]:
    """Return [{start_ms, end_ms, text}] using the calibrated recipe."""
    src = whisper_input(wav)
    out = wav.with_suffix("")
    segs = _whisper_pass(src, out, prompt)
    # Whisper feeds each 30s window the previous window's text — one bad
    # window can poison the whole rest of the file with a repeating loop.
    # Consecutive-dedupe can't catch alternating two-line loops (a 43-min
    # lecture once collapsed to "Okay."/one sentence × 1000 each), so
    # detect by unique-line ratio and redo without cross-window context.
    if len(segs) >= 30 and _uniq_ratio(segs) < 0.5:
        retry = _whisper_pass(src, out, prompt, extra=["-mc", "0"])
        if _uniq_ratio(retry) > _uniq_ratio(segs):
            segs = retry
    return dedupe_loops(segs)


def dedupe_loops(segs: list[dict]) -> list[dict]:
    """Collapse whisper hallucination loops (same text repeated 3+ times)."""
    out, streak = [], 0
    for s in segs:
        if out and s["text"] == out[-1]["text"]:
            streak += 1
            if streak >= 2:  # keep at most 2 consecutive identical lines
                out[-1]["end_ms"] = s["end_ms"]
                continue
        else:
            streak = 0
        out.append(s)
    return out


def ollama_model() -> str | None:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
        for want in LLM_PREFERRED:
            if want in names:
                return want
        return names[0] if names else None
    except Exception:
        return None


def ollama_chat(model: str, system: str, user: str, timeout=600) -> str:
    body = json.dumps({"model": model, "stream": False,
                       "options": {"temperature": 0.2, "num_ctx": 16384},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["message"]["content"]


def fmt_ts(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


STYLE_RULES = """Be COMPREHENSIVE: capture every distinct point, example, anecdote, and cited reference (scripture verses, book titles, names, numbers). Attribute statements to speakers by name ONLY using names that actually appear as speaker labels in the transcript. If no real speaker names are known, write "the speaker" — NEVER invent or borrow a name. Quote short key phrases verbatim in 'single quotes' where they carry the meaning. Bold the load-bearing term of each bullet with **. Every ts must be a timestamp copied from the transcript near that content. Do not invent content not in the transcript."""


def _schema_parts(tpl: dict, with_digest: bool):
    """(json-shape string, rules string) for a template's prompts."""
    shape = ['"digest": "2-3 sentence summary of this segment"' if with_digest
             else '"summary": "1-2 paragraphs separated by \\n\\n"']
    if tpl.get("has_actions"):
        shape.append('"action_items": [{"text": "...", "ts": "MM:SS"}]')
    if tpl.get("has_decisions"):
        shape.append('"decisions": [{"text": "...", "ts": "MM:SS"}]')
    fixed = [s for s in tpl["sections"] if not s.get("dynamic")]
    dynamic = [s for s in tpl["sections"] if s.get("dynamic")]
    if fixed:
        keys = ", ".join(f'"{s["title"]}": [{{"text": "...", "ts": "MM:SS"}}]'
                         for s in fixed)
        shape.append(f'"sections": {{{keys}}}')
    if dynamic:
        shape.append('"topics": [{"title": "...", "bullets": '
                     '[{"text": "...", "ts": "MM:SS"}]}]')
    rules = []
    if not with_digest:
        rules.append('The summary covers the themes and arc of the session; '
                     'when the leader is identifiable, open with who led it; '
                     'wrap 3-6 key phrases in **bold**.')
    if tpl.get("has_actions"):
        rules.append("Action items are concrete tasks someone must do — "
                     "EMPTY list if none (e.g. lectures); never invent tasks.")
    if tpl.get("has_decisions"):
        rules.append("Decisions are explicit agreements or conclusions — "
                     "empty list if none.")
    for s in fixed:
        rules.append(f'Section "{s["title"]}": {s["prompt"]} '
                     f'(empty list if nothing applies.)')
    for s in dynamic:
        rules.append(f'topics: {s["prompt"]} Each topic has 3-8 substantive '
                     f'full-sentence bullets.')
    return ",\n ".join(shape), " ".join(rules)


def build_note_system(tpl: dict) -> str:
    shape, rules = _schema_parts(tpl, with_digest=False)
    return (f'You are an expert meeting-notes writer using the '
            f'"{tpl["name"]}" template. You receive a meeting transcript with '
            f'[MM:SS] timestamps and speaker names. Reply with ONLY a JSON '
            f'object, no markdown fences, in exactly this shape:\n'
            f'{{{shape}}}\nRules: {rules} {STYLE_RULES}')


def build_chunk_system(tpl: dict) -> str:
    shape, rules = _schema_parts(tpl, with_digest=True)
    return (f'You extract structured notes from ONE SEGMENT of a longer '
            f'meeting transcript (with [MM:SS] timestamps and speaker names), '
            f'using the "{tpl["name"]}" template. Reply with ONLY a JSON '
            f'object, no markdown fences:\n{{{shape}}}\nRules: {rules} '
            f'{STYLE_RULES}')

REDUCE_SYSTEM = """You write the final summary of a long meeting from segment digests and topic titles. Reply with ONLY a JSON object, no markdown fences:
{"summary": "1-2 paragraphs separated by \\n\\n"}
Cover the themes and arc of the whole session, then the practical/applied content. Open with who led it ONLY if a real speaker name appears in the digests; otherwise begin "This session covered..." — never invent a name. Wrap 3-6 key phrases in **bold**. Do not invent content."""

CHUNK_CHARS = 9000  # ~2.3k tokens per map call — finer chunks = more detailed topics


def _llm_json(model: str, system: str, user: str, mdir: Path):
    for _ in range(2):
        try:
            reply = ollama_chat(model, system, user)
        except Exception as e:
            log(mdir, f"ollama error: {e}")
            return {"error": f"Ollama call failed: {e}"}
        obj = extract_json(reply)
        if obj:
            return obj
        user = "Your previous reply was not valid JSON. Reply with ONLY the JSON object.\n\n" + user
    return {"error": "LLM did not return valid JSON after 2 attempts."}


_INLINE_TS = re.compile(r"\s*\(\s*\d{1,2}:\d{2}(?:\s*[-–—,]\s*\d{1,2}:\d{2})*\s*\)")


def _strip_inline_ts(note: dict):
    """Models sometimes bake '(36:29-38:18)' ranges into bullet text even
    though ts is a separate field — strip them everywhere."""
    def clean(items):
        for it in items or []:
            if isinstance(it, dict) and it.get("text"):
                it["text"] = _INLINE_TS.sub("", it["text"]).strip()
    clean(note.get("action_items"))
    clean(note.get("decisions"))
    for sec in note.get("sections") or []:
        clean(sec.get("bullets"))
    for t in note.get("topics") or []:
        clean(t.get("bullets"))


def _ordered_sections(tpl: dict, collected: dict) -> list[dict]:
    """Fixed sections in template order, always present (even when empty)."""
    out = []
    for s in tpl["sections"]:
        if s.get("dynamic"):
            continue
        bullets, seen = [], set()
        for b in collected.get(s["title"], []) or []:
            key = (b.get("text") or "").strip().lower()
            if key and key not in seen:  # dedupe across map-reduce chunks
                seen.add(key)
                bullets.append(b)
        out.append({"title": s["title"], "bullets": bullets})
    return out


def generate_note(segs: list[dict], context: str, mdir: Path,
                  template_id: str = "general") -> dict:
    import note_templates
    tpl = note_templates.by_id(template_id)
    model = ollama_model()
    if not model:
        return {"error": "Ollama is not running — start it and click Regenerate."}
    lines = [f"[{fmt_ts(s['start_ms'])}] {s.get('speaker','Speaker')}: {s['text']}"
             for s in segs]
    transcript = "\n".join(lines)
    ctx_prefix = f"Meeting context: {context}\n\n" if context.strip() else ""
    named = sorted({s.get("speaker", "") for s in segs
                    if s.get("speaker") and not s["speaker"].startswith("Speaker ")})
    if named:
        ctx_prefix += f"Named speakers in this meeting: {', '.join(named)}\n\n"
    log(mdir, f"note: model={model} template={tpl['id']} chars={len(transcript)}")

    def finish(note):
        note["model"] = model
        note["template"] = tpl["id"]
        note["template_name"] = tpl["name"]
        if not tpl.get("has_actions"):
            note.pop("action_items", None)
        if not tpl.get("has_decisions"):
            note.pop("decisions", None)
        _strip_inline_ts(note)
        return note

    if len(transcript) <= CHUNK_CHARS + 4000:
        note = _llm_json(model, build_note_system(tpl),
                         ctx_prefix + "Transcript:\n" + transcript, mdir)
        if "summary" not in note:
            return note if note.get("error") else {"error": "note generation failed"}
        note["sections"] = _ordered_sections(tpl, note.get("sections") or {})
        return finish(note)

    # ---- map-reduce for long meetings (no truncation) ----
    chunks, cur, size = [], [], 0
    for ln in lines:
        cur.append(ln)
        size += len(ln) + 1
        if size >= CHUNK_CHARS:
            chunks.append("\n".join(cur))
            cur, size = [], 0
    if cur:
        chunks.append("\n".join(cur))
    log(mdir, f"note: map-reduce over {len(chunks)} chunks")

    chunk_system = build_chunk_system(tpl)
    digests, actions, decisions, topics = [], [], [], []
    section_acc: dict[str, list] = {}
    for i, chunk in enumerate(chunks):
        part = _llm_json(model, chunk_system,
                         ctx_prefix + f"Segment {i + 1} of {len(chunks)}:\n" + chunk, mdir)
        if part.get("error"):
            log(mdir, f"chunk {i + 1} failed: {part['error']}")
            continue
        digests.append(part.get("digest", ""))
        actions += part.get("action_items", []) or []
        decisions += part.get("decisions", []) or []
        topics += part.get("topics", []) or []
        for title, bullets in (part.get("sections") or {}).items():
            if isinstance(bullets, list):
                section_acc.setdefault(title, []).extend(bullets)
        log(mdir, f"chunk {i + 1}/{len(chunks)} ok")

    if not digests:
        return {"error": "all note chunks failed — is Ollama running?"}

    # merge consecutive chunks' topics that share a title
    merged = []
    for t in topics:
        if merged and merged[-1].get("title", "").strip().lower() == \
                t.get("title", "").strip().lower():
            merged[-1]["bullets"] = (merged[-1].get("bullets") or []) + (t.get("bullets") or [])
        else:
            merged.append(t)
    topics = merged

    digest_text = "Segment digests:\n" + "\n".join(f"- {d}" for d in digests if d) + \
        "\n\nTopic titles in order:\n" + "\n".join(f"- {t.get('title','')}" for t in topics)
    red = _llm_json(model, REDUCE_SYSTEM, ctx_prefix + digest_text, mdir)
    summary = red.get("summary", " ".join(digests)[:1500])

    return finish({"summary": summary, "action_items": actions,
                   "decisions": decisions, "topics": topics,
                   "sections": _ordered_sections(tpl, section_acc),
                   "chunks": len(chunks)})


def note_markdown(title: str, note: dict) -> str:
    if note.get("error"):
        return f"# {title}\n\n_AI Note unavailable: {note['error']}_\n"
    md = [f"# {title}", "", "## Summary", note.get("summary", ""), ""]
    if "action_items" in note:
        md += ["## Action items"]
        md += ([f"- [ ] {a['text']} ({a.get('ts','')})" for a in note.get("action_items") or []]
               or ["*No action items detected.*"]) + [""]
    if "decisions" in note:
        md += ["## Decisions"]
        md += ([f"- {d['text']} ({d.get('ts','')})" for d in note.get("decisions") or []]
               or ["*No decisions detected.*"]) + [""]
    for sec in note.get("sections") or []:
        md += [f"## {sec['title']}"]
        md += ([f"- {b['text']} ({b.get('ts','')})" for b in sec.get("bullets") or []]
               or ["*None noted.*"]) + [""]
    for t in note.get("topics", []):
        md += [f"## {t.get('title','Topic')}"] + \
            [f"- {b['text']} ({b.get('ts','')})" for b in t.get("bullets", [])] + [""]
    return "\n".join(md)


def process_meeting(mdir: Path):
    """Full pipeline for a meeting directory. Updates meeting.json status."""
    meta = json.loads((mdir / "meeting.json").read_text(encoding="utf-8"))
    try:
        context = meta.get("context", "")
        tracks = []  # (speaker, wav)

        # mic: numbered segments (pause/resume) — raw PCM (in-person) and/or
        # voice-processed CAF (online, echo-cancelled) — plus legacy mic.raw.
        mic_segs = sorted(list(mdir.glob("mic-*.raw")) + list(mdir.glob("mic-*.caf")),
                          key=lambda p: p.name) or \
            ([mdir / "mic.raw"] if (mdir / "mic.raw").exists() else [])
        if mic_segs:
            wavs = []
            for i, segf in enumerate(mic_segs):
                w = mdir / f"mic-part{i:03d}.wav"
                if segf.suffix == ".raw":
                    # mic_rate stamped at record start; legacy mic.raw was 16 kHz
                    rate = meta.get("mic_rate",
                                    16000 if segf.name == "mic.raw" else 48000)
                    ok = raw_to_wav(segf, w, in_rate=rate)
                else:
                    ok = to_wav(segf, w)
                if ok:
                    wavs.append(w)
            if len(wavs) == 1:
                wavs[0].rename(mdir / "mic.wav")
            elif wavs:
                lst = mdir / "miclist.txt"
                lst.write_text("".join(f"file '{w.resolve()}'\n" for w in wavs))
                run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-c", "copy", str(mdir / "mic.wav")])
                lst.unlink()
                for w in wavs:
                    w.unlink(missing_ok=True)
            if (mdir / "mic.wav").exists():
                tracks.append(("Me" if meta["mode"] == "online" else "Speaker 1",
                               mdir / "mic.wav"))

        sys_segs = sorted(mdir.glob("system-*.caf")) or \
            ([mdir / "system.caf"] if (mdir / "system.caf").exists() else [])
        if sys_segs:
            wavs = []
            for i, seg in enumerate(sys_segs):
                w = mdir / f"system-part{i:03d}.wav"
                if to_wav(seg, w):
                    wavs.append(w)
            if len(wavs) == 1:
                wavs[0].rename(mdir / "system.wav")
            elif wavs:
                lst = mdir / "syslist.txt"
                lst.write_text("".join(f"file '{w.resolve()}'\n" for w in wavs))
                run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-c", "copy", str(mdir / "system.wav")])
                lst.unlink()
                for w in wavs:
                    w.unlink(missing_ok=True)
            if (mdir / "system.wav").exists():
                tracks.append(("Others", mdir / "system.wav"))
        if (mdir / "upload.bin").exists():
            if to_wav(mdir / "upload.bin", mdir / "upload.wav"):
                tracks.append(("Speaker 1", mdir / "upload.wav"))

        if not tracks:
            raise RuntimeError("no audio tracks found")

        # playback file: mix all tracks
        if len(tracks) == 1:
            shutil.copyfile(tracks[0][1], mdir / "meeting.wav")
        else:
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(tracks[0][1]), "-i", str(tracks[1][1]),
                 "-filter_complex", "amix=inputs=2:duration=longest",
                 str(mdir / "meeting.wav")])

        dur = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "csv=p=0", str(mdir / "meeting.wav")]).stdout.strip()
        meta["duration_s"] = int(float(dur or 0))

        import diarize
        import voiceprints
        settings_f = APP_DIR / "data" / "settings.json"
        my_name = ""
        if settings_f.exists():
            my_name = json.loads(settings_f.read_text()).get("my_name", "")
        segs = []
        for orig_label, wav in tracks:
            # EVERY track is voice-verified. The online mic channel is only
            # *probably* the owner — in a live room it hears everyone, so
            # my_name is applied by voice match, never by assumption.
            mic_online = orig_label == "Me"
            label = "Speaker 1" if mic_online else orig_label
            log(mdir, f"transcribing {wav.name} (track: {orig_label})")
            track_segs = transcribe(wav, context)
            for s in track_segs:
                s["speaker"] = label
            wav16 = wav.with_name(wav.stem + ".16k.wav")
            if not wav16.exists():
                wav16 = whisper_input(wav)
            track_segs = drop_silent_segments(track_segs, wav16)
            if diarize.available():
                diar = diarize.diarize_wav(wav16)
                n_speakers = len({d[2] for d in diar})
                log(mdir, f"diarization {wav.name}: {len(diar)} turns, "
                          f"{n_speakers} speakers")
                if n_speakers > 1:
                    diarize.assign_speakers(track_segs, diar, label)
            details = {}
            try:
                hits, details = voiceprints.identify(
                    wav16, track_segs, log=lambda m: log(mdir, m))
                if hits:
                    log(mdir, f"voiceprints matched: {hits}")
            except Exception as e:
                log(mdir, f"voiceprint identify failed: {e}")
            if mic_online and my_name:
                # one unmatched cluster on the mic may still be the owner:
                # accept a slightly relaxed match, or no-prints fallback —
                # but never override a cluster that resembles someone else more
                left = sorted({s["speaker"] for s in track_segs
                               if s["speaker"].startswith("Speaker ")})
                if len(left) == 1:
                    best = details.get(left[0])
                    relaxed = voiceprints._threshold() - 0.12
                    if (not voiceprints.has_prints(my_name)) or \
                            (best and best[0] == my_name and best[1] >= relaxed):
                        for s in track_segs:
                            if s["speaker"] == left[0]:
                                s["speaker"] = my_name
                        log(mdir, f"mic cluster {left[0]} -> {my_name} "
                                  f"(owner fallback)")
            for s in track_segs:
                s["_track"] = orig_label
            segs.extend(track_segs)
        # NEVER hand an empty transcript to the note LLM — it hallucinates a
        # plausible note from nothing (happened 2026-08-05: fabricated recap
        # exported to Drive/Obsidian/calendar). Fail loudly instead.
        total_chars = sum(len(s["text"]) for s in segs)
        if total_chars < 200:
            raise RuntimeError(
                f"transcription produced almost no text ({total_chars} chars) "
                f"— audio conversion or capture problem; note NOT generated")
        segs.sort(key=lambda s: s["start_ms"])
        # unnamed clusters from different tracks must not share a label
        remap, counter = {}, 0
        for s in segs:
            track = s.pop("_track", "")
            if s["speaker"].startswith("Speaker "):
                key = (track, s["speaker"])
                if key not in remap:
                    counter += 1
                    remap[key] = f"Speaker {counter}"
                s["speaker"] = remap[key]
        (mdir / "transcript.json").write_text(
            json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")

        meta["status"] = "noting"
        (mdir / "meeting.json").write_text(json.dumps(meta), encoding="utf-8")

        note = generate_note(segs, context, mdir,
                             meta.get("template_id", "general"))
        (mdir / "note.json").write_text(
            json.dumps(note, ensure_ascii=False, indent=1), encoding="utf-8")
        (mdir / "note.md").write_text(
            note_markdown(meta.get("title", "Meeting"), note), encoding="utf-8")

        meta["status"] = "ready" if not note.get("error") else "ready_no_note"
        if meta["status"] == "ready":
            finish_extras(meta, mdir, note)
    except Exception as e:
        log(mdir, f"ERROR: {e}")
        meta["status"] = "error"
        meta["error"] = str(e)
    (mdir / "meeting.json").write_text(json.dumps(meta), encoding="utf-8")


def finish_extras(meta: dict, mdir: Path, note: dict):
    """After the note is ready: auto-export the Google Doc and write the
    debrief back onto the calendar event. Both best-effort."""
    import time as _time

    import gcal_writeback
    import gdoc_export
    try:
        settings_file = APP_DIR / "data" / "settings.json"
        gdir = None
        if settings_file.exists():
            gdir = json.loads(settings_file.read_text()).get("gdoc_dir")
        path = gdoc_export.export_note(
            meta.get("title", "Meeting"),
            _time.strftime("%b %d, %Y %I:%M %p",
                           _time.localtime(meta.get("created_at", 0))),
            note, Path(gdir) if gdir else None)
        meta["gdoc_path"] = str(path)
        log(mdir, f"gdoc exported: {path}")
    except Exception as e:
        log(mdir, f"gdoc export failed: {e}")

    try:
        import obsidian_export
        note_md = (mdir / "note.md").read_text(encoding="utf-8") \
            if (mdir / "note.md").exists() else note_markdown(
                meta.get("title", "Meeting"), note)
        opath = obsidian_export.export_note(meta, note, note_md, mdir)
        if opath:
            meta["obsidian_path"] = str(opath)
            log(mdir, f"obsidian exported: {opath}")
    except Exception as e:
        log(mdir, f"obsidian export failed: {e}")

    if meta.get("calendar_uid"):
        result = gcal_writeback.write_debrief(
            meta.get("title", ""), meta.get("created_at", 0),
            note, meta.get("gdoc_path", ""))
        meta["calendar_writeback"] = result
        log(mdir, f"calendar writeback: {result}")
