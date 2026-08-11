"""LocalFellow MVP server — meetings, recording control, transcript, AI notes.
Run via ./run.sh (creates venv with fastapi/uvicorn)."""
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import calendar_ics
import gdoc_export
import pipeline
import recorder

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data" / "meetings"
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = APP_DIR / "data" / "settings.json"

app = FastAPI(title="LocalFellow")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """The UI ships as plain static files that change often — never let the
    browser run a stale app.js against a newer backend ("page not loading")."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def recover_interrupted_recordings():
    """If the server died mid-recording, capture processes are orphaned and the
    meeting is stuck on 'recording'. Kill strays and salvage the segments —
    raw-PCM capture means everything up to the crash is intact (PRD FR-1.4)."""
    import subprocess
    # belt-and-braces: at startup NO capture process should exist. A rogue
    # ffmpeg once recorded into a processed meeting for 4 days (8.6 GB).
    # ONLY capture processes — a blanket data-dir pkill once killed a
    # legitimate reprocess's ffmpeg conversions mid-flight.
    for pat in (f"avfoundation.*{DATA}", f"systemaudio.*{DATA}",
                f"voicemic.*{DATA}"):
        subprocess.run(["pkill", "-INT", "-f", pat], capture_output=True)
    time.sleep(1)
    for d in DATA.iterdir():
        f = d / "meeting.json"
        if not f.exists():
            continue
        meta = json.loads(f.read_text(encoding="utf-8"))
        status = meta.get("status")
        if status in ("processing", "noting"):
            # worker died in a restart — a meeting must never zombie in a
            # busy status (it also blocks queued updates forever)
            meta["status"] = "processing"
            meta["recovered"] = True
            f.write_text(json.dumps(meta), encoding="utf-8")
            threading.Thread(target=pipeline.process_meeting, args=(d,),
                             daemon=True).start()
            continue
        if status not in ("recording", "paused"):
            continue
        subprocess.run(["pkill", "-INT", "-f", str(d)], capture_output=True)
        time.sleep(1)
        if list(d.glob("mic-*.raw")) or list(d.glob("mic-*.caf")) or \
                (d / "mic.raw").exists():
            meta["status"] = "processing"
            meta["recovered"] = True
            f.write_text(json.dumps(meta), encoding="utf-8")
            threading.Thread(target=pipeline.process_meeting, args=(d,),
                             daemon=True).start()
        else:
            meta["status"] = "error"
            meta["error"] = "recording interrupted before any audio was captured"
            f.write_text(json.dumps(meta), encoding="utf-8")


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}


PEOPLE_FILE = APP_DIR / "data" / "people.json"


def add_people(names):
    known = json.loads(PEOPLE_FILE.read_text(encoding="utf-8")) \
        if PEOPLE_FILE.exists() else []
    for n in names:
        n = (n or "").strip()
        if n and n not in known and not n.startswith("Speaker "):
            known.append(n)
    PEOPLE_FILE.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")


@app.get("/api/people")
def get_people():
    return json.loads(PEOPLE_FILE.read_text(encoding="utf-8")) \
        if PEOPLE_FILE.exists() else []


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    vp = APP_DIR / "data" / "voiceprints.json"
    return {"ics_url_set": bool(s.get("ics_url")),
            "my_name": s.get("my_name", ""),
            "has_voiceprints": vp.exists() and vp.stat().st_size > 10}


@app.post("/api/settings")
def set_settings(body: dict):
    s = load_settings()
    for key in ("ics_url", "my_name", "ask_model", "retention_days",
                "obsidian_dir"):
        if key in body:
            s[key] = str(body[key]).strip()
    SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
    return {"ok": True}


AUDIO_PATTERNS = ("meeting.wav", "mic*.raw", "mic*.caf", "mic*.wav",
                  "system*.caf", "system*.wav", "upload.*", "*.16k.wav")


def retention_sweep():
    """Daily: delete AUDIO (not transcripts/notes) of meetings older than
    retention_days (default 30; set 0 in settings.json to keep forever),
    and fully purge trash entries older than the same window."""
    import shutil
    try:
        days = int(load_settings().get("retention_days", 30) or 0)
    except ValueError:
        days = 30
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    purged = 0
    for d in DATA.iterdir():
        f = d / "meeting.json"
        if not f.exists():
            continue
        meta = json.loads(f.read_text(encoding="utf-8"))
        if meta.get("audio_purged") or meta.get("created_at", 0) > cutoff or \
                not str(meta.get("status", "")).startswith("ready"):
            continue
        removed = 0
        for pat in AUDIO_PATTERNS:
            for p in d.glob(pat):
                p.unlink(missing_ok=True)
                removed += 1
        if removed:
            meta["audio_purged"] = True
            f.write_text(json.dumps(meta), encoding="utf-8")
            purged += 1
    trash = APP_DIR / "data" / "trash"
    trashed = 0
    if trash.exists():
        for d in trash.iterdir():
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                trashed += 1
    if purged or trashed:
        print(f"retention: audio purged from {purged} meetings, "
              f"{trashed} trash entries removed (> {days} days)")


START_TIME = time.time()


def _anything_busy() -> bool:
    for d in DATA.iterdir():
        f = d / "meeting.json"
        if f.exists():
            st = json.loads(f.read_text(encoding="utf-8")).get("status")
            if st in ("recording", "paused", "processing", "noting"):
                return True
    return False


def _code_changed() -> bool:
    newest = 0.0
    for p in list(APP_DIR.glob("*.py")) + list((APP_DIR / "static").glob("*")):
        newest = max(newest, p.stat().st_mtime)
    return newest > START_TIME


@app.get("/api/update_status")
def update_status():
    s = load_settings()
    available = _code_changed()
    pending = bool(s.get("pending_update"))
    if pending and not available:
        # stale flag with nothing to apply — the banner must NEVER show
        # unless a real update exists
        s.pop("pending_update", None)
        SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
        pending = False
    return {"update_available": available,
            "pending": pending,
            "busy": _anything_busy(),
            "running_since": int(START_TIME)}


@app.post("/api/update")
def apply_update():
    """Restart the server to load updated code — but NEVER while a recording
    or processing is in flight. If busy, the update queues and applies itself
    the moment everything is idle (launchd relaunches us)."""
    import os
    s = load_settings()
    if not _code_changed():
        s.pop("pending_update", None)
        SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
        return {"queued": False, "message": "Already up to date — nothing to apply."}
    if _anything_busy():
        s["pending_update"] = True
        SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
        return {"queued": True,
                "message": "Recording/processing in progress — the update "
                           "will apply automatically when it finishes."}
    s.pop("pending_update", None)
    SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
    threading.Timer(0.7, lambda: os._exit(0)).start()
    return {"queued": False, "message": "Restarting now — back in ~10 seconds."}


@app.on_event("startup")
def watch_pending_update():
    def loop():
        import os
        while True:
            time.sleep(30)
            try:
                s = load_settings()
                if s.get("pending_update") and not _anything_busy():
                    s.pop("pending_update", None)
                    SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
                    print("pending update: all idle — restarting")
                    os._exit(0)
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()


@app.on_event("startup")
def schedule_retention():
    def loop():
        while True:
            try:
                retention_sweep()
            except Exception as e:
                print("retention sweep failed:", e)
            time.sleep(86400)
    threading.Thread(target=loop, daemon=True).start()


@app.get("/api/calendar/today")
def calendar_today():
    url = load_settings().get("ics_url")
    if not url:
        return {"connected": False, "events": []}
    try:
        return {"connected": True, "events": calendar_ics.today_events(url)}
    except Exception as e:
        return {"connected": True, "error": str(e), "events": []}


@app.post("/api/calendar/record")
def calendar_record(body: dict):
    """Create a meeting prefilled from a calendar event and start recording."""
    ev = body.get("event") or {}
    mid = uuid.uuid4().hex[:12]
    d = DATA / mid
    d.mkdir()
    add_people(ev.get("attendees") or [])
    attendees = ", ".join(ev.get("attendees") or [])
    meta = {"id": mid, "title": ev.get("title") or "Calendar meeting",
            "mode": body.get("mode", "online"),
            "context": f"Meeting with: {attendees}" if attendees else "",
            "status": "idle", "created_at": int(time.time()), "duration_s": 0,
            "calendar_uid": ev.get("uid", "")}
    (d / "meeting.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "agenda.json").write_text(json.dumps(
        {"talking_points": "", "action_items": "", "notepad": ""}), encoding="utf-8")
    res = start_recording(mid)
    if res.get("already_recording"):
        import shutil
        shutil.rmtree(d, ignore_errors=True)  # discard the never-started shell
    return res


def mdir(mid: str) -> Path:
    d = (DATA / mid).resolve()
    if not d.is_relative_to(DATA) or not d.exists():
        raise FileNotFoundError(mid)
    return d


def load(mid: str) -> dict:
    return json.loads((mdir(mid) / "meeting.json").read_text(encoding="utf-8"))


def save(mid: str, meta: dict):
    (mdir(mid) / "meeting.json").write_text(json.dumps(meta), encoding="utf-8")


def run_pipeline_bg(mid: str):
    threading.Thread(target=pipeline.process_meeting, args=(mdir(mid),),
                     daemon=True).start()


@app.get("/api/meetings")
def list_meetings():
    out = []
    for d in DATA.iterdir():
        f = d / "meeting.json"
        if f.exists():
            out.append(json.loads(f.read_text(encoding="utf-8")))
    out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return out


@app.post("/api/meetings")
def create_meeting(body: dict):
    mid = uuid.uuid4().hex[:12]
    d = DATA / mid
    d.mkdir()
    meta = {"id": mid,
            "title": body.get("title") or time.strftime("Meeting — %b %d, %I:%M %p"),
            "mode": body.get("mode", "inperson"),
            "context": body.get("context", ""),
            "status": "idle", "created_at": int(time.time()), "duration_s": 0}
    (d / "meeting.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "agenda.json").write_text(json.dumps(
        {"talking_points": "", "action_items": "", "notepad": ""}), encoding="utf-8")
    return meta


@app.get("/api/meetings/{mid}")
def get_meeting(mid: str):
    d = mdir(mid)
    meta = load(mid)
    out = {"meeting": meta,
           "agenda": json.loads((d / "agenda.json").read_text(encoding="utf-8"))
           if (d / "agenda.json").exists() else {},
           "transcript": json.loads((d / "transcript.json").read_text(encoding="utf-8"))
           if (d / "transcript.json").exists() else [],
           "note": json.loads((d / "note.json").read_text(encoding="utf-8"))
           if (d / "note.json").exists() else None,
           "note_md": (d / "note.md").read_text(encoding="utf-8")
           if (d / "note.md").exists() else ""}
    return out


@app.patch("/api/meetings/{mid}")
def update_meeting(mid: str, body: dict):
    meta = load(mid)
    for k in ("title", "context", "template_id"):
        if k in body:
            meta[k] = body[k]
    if "mode" in body and meta["status"] == "idle":
        meta["mode"] = body["mode"]
    save(mid, meta)
    return meta


@app.get("/api/templates")
def list_templates():
    import note_templates
    return [{"id": t["id"], "name": t["name"], "builtin": t.get("builtin", False)}
            for t in note_templates.all_templates()]


@app.delete("/api/meetings/{mid}")
def delete_meeting(mid: str):
    """Soft delete: move the meeting bundle to data/trash/ (recoverable).
    Kills any capture processes still writing into it first — deleting a
    recording meeting must not leave recorders running into a ghost dir."""
    import shutil
    import subprocess
    d = mdir(mid)
    recorder.stop(mid)
    subprocess.run(["pkill", "-INT", "-f", str(d)], capture_output=True)
    time.sleep(1)
    trash = APP_DIR / "data" / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(trash / f"{mid}-{int(time.time())}"))
    return {"ok": True}


@app.post("/api/merge")
def merge_meetings(body: dict):
    """Merge N meetings (e.g. accidental fragments of one class) into one:
    audio concatenated, transcripts stitched with offset timestamps (no
    re-transcription), ONE recap regenerated over the whole. Originals go
    to trash after success."""
    import shutil
    import subprocess
    import uuid as _uuid
    ids = body.get("meeting_ids") or []
    if len(ids) < 2:
        return JSONResponse({"error": "need at least 2 meeting_ids"}, status_code=400)
    order = sorted(((i, load(i)) for i in ids),
                   key=lambda x: x[1].get("created_at", 0))
    base = order[0][1]
    mid = _uuid.uuid4().hex[:12]
    d = DATA / mid
    d.mkdir()
    meta = {"id": mid, "title": base.get("title", "Merged meeting"),
            "mode": base.get("mode", "inperson"),
            "context": next((m.get("context", "") for _, m in order
                             if m.get("context")), ""),
            "template_id": base.get("template_id", "general"),
            "status": "processing", "created_at": base.get("created_at",
                                                           int(time.time())),
            "duration_s": 0, "merged_from": [i for i, _ in order],
            "calendar_uid": next((m.get("calendar_uid", "") for _, m in order
                                  if m.get("calendar_uid")), ""),
            "mic_rate": 48000}
    save(mid, meta)
    (d / "agenda.json").write_text(json.dumps(
        {"talking_points": "", "action_items": "", "notepad": ""}), encoding="utf-8")

    def work():
        try:
            offset_ms, all_segs, parts = 0, [], []
            for i, (oid, om) in enumerate(order):
                od = DATA / oid
                src = od / "meeting.wav"
                if not src.exists():
                    continue
                w = d / f"part{i:03d}.wav"
                pipeline.to_wav(src, w)  # normalize everything to 48 kHz mono
                dur = pipeline.run(["ffprobe", "-v", "error", "-show_entries",
                                    "format=duration", "-of", "csv=p=0",
                                    str(w)]).stdout.strip()
                dur_ms = int(float(dur or 0) * 1000)
                tf = od / "transcript.json"
                if tf.exists():
                    for s in json.loads(tf.read_text(encoding="utf-8")):
                        all_segs.append({**s,
                                         "start_ms": s["start_ms"] + offset_ms,
                                         "end_ms": s["end_ms"] + offset_ms})
                offset_ms += dur_ms
                parts.append(w)
            if not parts:
                raise RuntimeError("no audio found in the selected meetings")
            lst = d / "parts.txt"
            lst.write_text("".join(f"file '{w.resolve()}'\n" for w in parts))
            pipeline.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                          "-f", "concat", "-safe", "0", "-i", str(lst),
                          "-c", "copy", str(d / "meeting.wav")])
            lst.unlink()
            for w in parts:
                w.unlink(missing_ok=True)
            meta["duration_s"] = offset_ms // 1000
            (d / "transcript.json").write_text(
                json.dumps(all_segs, ensure_ascii=False, indent=1),
                encoding="utf-8")
            meta["status"] = "noting"
            save(mid, meta)
            note = pipeline.generate_note(all_segs, meta.get("context", ""), d,
                                          meta.get("template_id", "general"))
            (d / "note.json").write_text(
                json.dumps(note, ensure_ascii=False, indent=1), encoding="utf-8")
            (d / "note.md").write_text(
                pipeline.note_markdown(meta["title"], note), encoding="utf-8")
            meta["status"] = "ready" if not note.get("error") else "ready_no_note"
            if meta["status"] == "ready":
                pipeline.finish_extras(meta, d, note)
            # originals to trash only after full success
            trash = APP_DIR / "data" / "trash"
            trash.mkdir(parents=True, exist_ok=True)
            for oid, _ in order:
                subprocess.run(["pkill", "-INT", "-f", str(DATA / oid)],
                               capture_output=True)
                shutil.move(str(DATA / oid),
                            str(trash / f"{oid}-merged-{int(time.time())}"))
        except Exception as e:
            meta["status"] = "error"
            meta["error"] = f"merge failed: {e}"
        save(mid, meta)

    threading.Thread(target=work, daemon=True).start()
    return meta


@app.post("/api/quickstart")
def quickstart(body: dict):
    """One call for the menu bar: create a meeting and start recording."""
    m = create_meeting({"mode": body.get("mode", "inperson")})
    return start_recording(m["id"])


@app.post("/api/meetings/{mid}/start")
def start_recording(mid: str):
    meta = load(mid)
    if meta["status"] == "recording":
        return meta
    # one recording at a time — a second Start returns the active meeting
    # (today's field bug: four concurrent capture attempts piled up)
    for d2 in DATA.iterdir():
        f2 = d2 / "meeting.json"
        if d2.name != mid and f2.exists():
            other = json.loads(f2.read_text(encoding="utf-8"))
            if other.get("status") in ("recording", "paused"):
                other["already_recording"] = True
                return other
    tracks = recorder.start(mid, mdir(mid), meta["mode"])
    meta["status"] = "recording"
    meta["record_started_at"] = int(time.time())
    meta["tracks"] = tracks
    meta["mic_rate"] = 48000
    save(mid, meta)
    return meta


@app.post("/api/meetings/{mid}/pause")
def pause_recording(mid: str):
    meta = load(mid)
    if meta["status"] != "recording":
        return meta
    recorder.stop(mid)
    meta["recorded_s"] = meta.get("recorded_s", 0) + \
        int(time.time()) - meta.get("record_started_at", int(time.time()))
    meta["status"] = "paused"
    save(mid, meta)
    return meta


@app.post("/api/meetings/{mid}/resume")
def resume_recording(mid: str):
    meta = load(mid)
    if meta["status"] != "paused":
        return meta
    meta["tracks"] = recorder.start(mid, mdir(mid), meta["mode"])
    meta["status"] = "recording"
    meta["record_started_at"] = int(time.time())
    save(mid, meta)
    return meta


@app.post("/api/meetings/{mid}/stop")
def stop_recording(mid: str):
    meta = load(mid)
    recorder.stop(mid)
    if meta["status"] == "recording":
        meta["recorded_s"] = meta.get("recorded_s", 0) + \
            int(time.time()) - meta.get("record_started_at", int(time.time()))
    meta["status"] = "processing"
    save(mid, meta)
    run_pipeline_bg(mid)
    return meta


@app.post("/api/meetings/{mid}/upload")
async def upload(mid: str, file: UploadFile):
    d = mdir(mid)
    dest = d / "upload.bin"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    meta = load(mid)
    meta["status"] = "processing"
    meta["source_filename"] = file.filename
    save(mid, meta)
    run_pipeline_bg(mid)
    return meta


@app.post("/api/meetings/{mid}/agenda")
def save_agenda(mid: str, body: dict):
    (mdir(mid) / "agenda.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@app.post("/api/meetings/{mid}/rename_speaker")
def rename_speaker(mid: str, body: dict):
    """body: {from, to, segment_index (optional — omit for all segments)}"""
    d = mdir(mid)
    segs = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
    idx = body.get("segment_index")
    for i, s in enumerate(segs):
        if idx is not None and i != idx:
            continue
        if s["speaker"] == body["from"] or idx == i:
            s["speaker"] = body["to"]
    (d / "transcript.json").write_text(
        json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    add_people([body["to"]])
    # implicit voice enrollment: an all-lines rename to a real name teaches
    # the voiceprint store, so this person is auto-labelled in future meetings
    to = body["to"].strip()
    if idx is None and to and not to.startswith("Speaker ") and \
            to not in ("Me", "Others"):
        def harvest():
            import voiceprints
            try:
                n = voiceprints.harvest_from_meeting(d, body["from"], to)
                pipeline.log(d, f"voiceprints harvested for {to}: {n}")
            except Exception as e:
                pipeline.log(d, f"voiceprint harvest failed: {e}")
        threading.Thread(target=harvest, daemon=True).start()
    return {"ok": True, "transcript": segs}


@app.post("/api/meetings/{mid}/regenerate_note")
def regenerate_note(mid: str):
    d = mdir(mid)
    meta = load(mid)
    if not (d / "transcript.json").exists():
        return JSONResponse({"error": "no transcript yet"}, status_code=400)

    def work():
        segs = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
        note = pipeline.generate_note(segs, meta.get("context", ""), d,
                                      meta.get("template_id", "general"))
        (d / "note.json").write_text(json.dumps(note, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        (d / "note.md").write_text(pipeline.note_markdown(meta.get("title", "Meeting"),
                                                          note), encoding="utf-8")
        meta["status"] = "ready" if not note.get("error") else "ready_no_note"
        if meta["status"] == "ready":
            pipeline.finish_extras(meta, d, note)
        save(mid, meta)

    meta["status"] = "noting"
    save(mid, meta)
    threading.Thread(target=work, daemon=True).start()
    return meta


def _record_mic(seconds: int) -> Path | None:
    import subprocess
    tmp = APP_DIR / "data" / "enroll.wav"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "avfoundation", "-i", ":0", "-t", str(seconds),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(tmp)])
    return tmp if tmp.exists() and tmp.stat().st_size > 32000 else None


def _quality(wav: Path) -> tuple[float, str]:
    """(mean_volume_db, verdict) — did we actually capture speech?"""
    mv = pipeline.mean_volume_db(wav)
    if mv < -45:
        return mv, "silent"
    if mv < -34:
        return mv, "quiet"
    return mv, "good"


@app.get("/api/voices")
def list_voices():
    import voiceprints
    return {"people": voiceprints.list_people(),
            "my_name": load_settings().get("my_name", "")}


@app.post("/api/voices/enroll")
def voices_enroll(body: dict):
    """Record ~20 s from the mic and store voiceprints for `name`
    (defaults to the my_name setting). Returns audio-quality feedback."""
    import voiceprints
    name = (body.get("name") or load_settings().get("my_name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    tmp = _record_mic(int(body.get("seconds", 20)))
    if tmp is None:
        return JSONResponse({"error": "mic recording failed — check microphone "
                             "permission"}, status_code=500)
    mv, verdict = _quality(tmp)
    if verdict == "silent":
        tmp.unlink(missing_ok=True)
        return JSONResponse({"error": f"no voice detected (level {mv:.0f} dB) — "
                             "speak during the countdown and try again"},
                            status_code=422)
    audio = voiceprints._read_wav16(tmp)
    chunks = [audio[i:i + 16000 * 5] for i in range(0, len(audio), 16000 * 5)
              if len(audio[i:i + 16000 * 5]) >= 16000 * 2]
    n = voiceprints.enroll_from_samples(name, chunks)
    tmp.unlink(missing_ok=True)
    add_people([name])
    return {"ok": True, "name": name, "prints_added": n,
            "level_db": round(mv, 1), "quality": verdict}


@app.post("/api/voices/test")
def voices_test(body: dict):
    """Record ~5 s and report who the voice matches (recognition self-test)."""
    import voiceprints
    tmp = _record_mic(int(body.get("seconds", 5)))
    if tmp is None:
        return JSONResponse({"error": "mic recording failed"}, status_code=500)
    mv, verdict = _quality(tmp)
    if verdict == "silent":
        tmp.unlink(missing_ok=True)
        return {"match": None, "score": 0, "quality": "silent",
                "message": f"No voice detected (level {mv:.0f} dB)."}
    name, score = voiceprints.match_sample(voiceprints._read_wav16(tmp))
    tmp.unlink(missing_ok=True)
    thresh = voiceprints._threshold()
    recognized = name if score >= thresh else None
    return {"match": recognized, "candidate": name, "score": round(score, 2),
            "threshold": thresh, "quality": verdict,
            "message": (f"Recognized as {name} (confidence {score:.2f})."
                        if recognized else
                        f"Not recognized (closest: {name} at {score:.2f}, "
                        f"needs ≥ {thresh}).")}


@app.delete("/api/voices/{name}")
def voices_delete(name: str):
    import voiceprints
    return {"ok": voiceprints.delete_person(name)}


@app.post("/api/enroll_voice")
def enroll_voice(body: dict):
    """Legacy alias for /api/voices/enroll."""
    return voices_enroll(body)


@app.post("/api/ask")
def ask_endpoint(body: dict):
    import ask as ask_mod
    q = (body.get("question") or "").strip()
    if not q:
        return JSONResponse({"error": "empty question"}, status_code=400)
    return ask_mod.ask(q, body.get("history"))


@app.get("/api/search")
def search_endpoint(q: str = ""):
    import meeting_tools
    if not q.strip():
        return []
    return meeting_tools.search_meetings(q, limit=20)


@app.post("/api/meetings/{mid}/export_gdoc")
def export_gdoc(mid: str):
    d = mdir(mid)
    if not (d / "note.json").exists():
        return JSONResponse({"error": "no AI note yet"}, status_code=400)
    meta = load(mid)
    note = json.loads((d / "note.json").read_text(encoding="utf-8"))
    if note.get("error"):
        return JSONResponse({"error": "AI note has an error — regenerate first"},
                            status_code=400)
    out_dir = load_settings().get("gdoc_dir")
    try:
        path = gdoc_export.export_note(
            meta.get("title", "Meeting"),
            time.strftime("%b %d, %Y %I:%M %p", time.localtime(meta.get("created_at", 0))),
            note, Path(out_dir) if out_dir else None)
    except Exception as e:
        return JSONResponse({"error": f"export failed: {e}"}, status_code=500)
    return {"ok": True, "path": str(path), "folder": str(path.parent)}


@app.get("/api/meetings/{mid}/audio")
def audio(mid: str):
    f = mdir(mid) / "meeting.wav"
    if not f.exists():
        return JSONResponse({"error": "no audio"}, status_code=404)
    return FileResponse(f, media_type="audio/wav")


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")
