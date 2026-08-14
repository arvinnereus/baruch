#!/usr/bin/env python3
"""Process one meeting in its own OS process.

Why this exists: the pipeline's diarization and speaker-embedding work is
CPU-bound Python that holds the GIL, so running it inside the web server made
the whole UI unresponsive for as long as a recording took to process — the app
appeared to hang, mid-class, three times. A separate process cannot starve the
server no matter how long it runs.

It also survives a server restart, so a queued update no longer abandons a
half-processed meeting: the server comes back, sees the worker is still alive,
and leaves it to finish.

    ./.venv/bin/python worker.py <meeting_dir>
"""
import datetime
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import pipeline

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data" / "meetings"
LOCK = APP_DIR / "data" / "automerge.lock"
BUSY = ("recording", "paused", "processing", "noting")


def _patch(f: Path, **fields):
    """Merge fields into meeting.json without clobbering the pipeline's own
    writes (re-read immediately before writing)."""
    try:
        meta = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return
    for k, v in fields.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    f.write_text(json.dumps(meta), encoding="utf-8")


def _settings() -> dict:
    f = APP_DIR / "data" / "settings.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _same_meeting(a: dict, b: dict) -> bool:
    """Two recordings are parts of ONE meeting only if they are the same day
    AND share a calendar event, or (failing that) an exact title. Deliberately
    strict: silently merging two genuinely different meetings is far worse
    than leaving two parts separate."""
    if not a.get("created_at") or not b.get("created_at"):
        return False
    if datetime.date.fromtimestamp(a["created_at"]) != \
            datetime.date.fromtimestamp(b["created_at"]):
        return False
    ua, ub = a.get("calendar_uid"), b.get("calendar_uid")
    if ua and ub:
        return ua == ub
    ta, tb = (a.get("title") or "").strip(), (b.get("title") or "").strip()
    return bool(ta) and ta == tb


def _siblings(me_id: str, me: dict) -> list[tuple[str, dict]]:
    out = []
    for f in DATA.glob("*/meeting.json"):
        if f.parent.name == me_id:
            continue
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _same_meeting(me, m):
            out.append((f.parent.name, m))
    return out


def auto_merge(d: Path, meta: dict):
    """Silently merge this finished recording with the other parts of the same
    meeting recorded today. Runs in the worker, so waiting for a sibling that
    is still processing costs the server nothing."""
    # default OFF until the end-to-end test has run against real recordings:
    # silently combining a user's meetings on untested code is not a risk
    # worth taking. Set "auto_merge": true in data/settings.json to enable.
    if not _settings().get("auto_merge", False) or meta.get("no_automerge"):
        return
    me_id = d.name
    if not _siblings(me_id, meta):
        return

    # serialize: two parts finishing together must not merge each other
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime < 3600:
            print("auto-merge: another part is merging, skipping")
            return
        LOCK.unlink(missing_ok=True)  # stale lock from a killed worker
        return auto_merge(d, meta)

    try:
        # a sibling may still be processing — wait for it rather than leaving
        # the day split, which is the whole point of the feature
        deadline = time.time() + 1800
        while time.time() < deadline:
            sibs = _siblings(me_id, meta)
            if not any(m.get("status") in BUSY for _, m in sibs):
                break
            time.sleep(20)
        ready = [(i, m) for i, m in _siblings(me_id, meta)
                 if str(m.get("status", "")).startswith("ready")]
        if not ready:
            return
        ids = [i for i, _ in ready] + [me_id]
        print(f"auto-merge: combining {len(ids)} parts -> {ids}")
        req = urllib.request.Request(
            "http://127.0.0.1:8377/api/merge",
            data=json.dumps({"meeting_ids": ids}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3600) as r:
            print("auto-merge result:", json.load(r).get("id"))
    except Exception as e:
        print(f"auto-merge skipped: {e}")  # never fail the recording over this
    finally:
        LOCK.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: worker.py <meeting_dir>", file=sys.stderr)
        return 2
    d = Path(sys.argv[1]).resolve()
    f = d / "meeting.json"
    if not f.exists():
        print(f"no meeting at {d}", file=sys.stderr)
        return 2

    # claim the meeting: the server checks this pid to tell "still working"
    # from "died in a restart", so it must be written by the worker itself
    _patch(f, worker_pid=os.getpid())
    try:
        pipeline.process_meeting(d)
        _patch(f, worker_pid=None)
        meta = json.loads(f.read_text(encoding="utf-8"))
        if str(meta.get("status", "")).startswith("ready"):
            auto_merge(d, meta)
        return 0
    except Exception as e:
        # process_meeting records its own failures, but a crash in its error
        # handling must still never leave a meeting stuck on "processing"
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            if meta.get("status") in ("processing", "noting"):
                _patch(f, status="error", error=f"processing failed: {e}",
                       worker_pid=None)
            else:
                _patch(f, worker_pid=None)
        except Exception:
            pass
        print(f"worker failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
