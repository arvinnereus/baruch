"""Recording process management: mic via ffmpeg (crash-safe raw PCM),
system audio via the ScreenCaptureKit `systemaudio` tool."""
import signal
import subprocess
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SYSTEMAUDIO = APP_DIR / "systemaudio"
VOICEMIC = APP_DIR / "voicemic"

_procs: dict[str, list[subprocess.Popen]] = {}


def start(meeting_id: str, mdir: Path, mode: str) -> list[str]:
    """mode: 'inperson' (mic only) or 'online' (mic + system audio).
    Each start/resume opens a NEW numbered segment (mic-001.raw, mic-002.raw…)
    so pauses are gaps between segments — concatenated at processing time."""
    seg = len(list(mdir.glob("mic-*.raw"))) + len(list(mdir.glob("mic-*.caf"))) + 1
    procs, notes = [], []
    if mode == "online" and VOICEMIC.exists():
        # Online: voice-processed capture — echo cancellation (no headphones
        # needed), AGC, noise suppression
        mic = subprocess.Popen(
            [str(VOICEMIC), str(mdir / f"mic-{seg:03d}.caf")],
            stderr=open(mdir / "mic.log", "a"))
        notes.append("mic (echo-cancelled)")
    else:
        # In-person: RAW 48 kHz s16le — calibration showed voice processing
        # can gate distant far-field speech; crash-safe headerless format
        mic = subprocess.Popen(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "avfoundation", "-i", ":0",
             "-ar", "48000", "-ac", "1", "-f", "s16le",
             str(mdir / f"mic-{seg:03d}.raw")],
            stderr=open(mdir / "mic.log", "a"))
        notes.append("mic")
    procs.append(mic)

    if mode == "online":
        if SYSTEMAUDIO.exists():
            sys_p = subprocess.Popen(
                [str(SYSTEMAUDIO), str(mdir / f"system-{seg:03d}.caf")],
                stderr=open(mdir / "system.log", "a"))
            procs.append(sys_p)
            notes.append("system")
        else:
            notes.append("system-UNAVAILABLE (systemaudio not built)")

    _procs[meeting_id] = procs
    return notes


def stop(meeting_id: str, timeout=8) -> None:
    """Signal all capture processes and wait for them to finalize files."""
    procs = _procs.pop(meeting_id, [])
    for p in procs:
        try:
            p.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
    deadline = time.time() + timeout
    for p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()


def is_recording(meeting_id: str) -> bool:
    return meeting_id in _procs
