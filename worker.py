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
import json
import os
import sys
from pathlib import Path

import pipeline

APP_DIR = Path(__file__).resolve().parent


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
