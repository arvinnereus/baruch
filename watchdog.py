#!/usr/bin/env python3
"""Check Baruch is actually working, repair what it can, and say so loudly.

Written after 2026-08-14, when two outages ran unnoticed: Ollama stopped (a
class would have transcribed and produced no AI note, silently) and the server
could not start at all, looping on a dead interpreter path for hours. Neither
surfaced anywhere — the app's offline banner only exists inside a browser tab,
which is closed precisely when you are in the meeting being recorded.

Runs from a LaunchAgent every few minutes:
  1. is the server answering?      no -> kick the LaunchAgent
  2. is Ollama answering?          no -> start it
  3. any meeting stuck mid-process? -> restart its worker
Anything it cannot fix becomes a macOS notification. Notifications are
deduplicated, so a persistent fault does not nag every five minutes, but a
fault that clears and returns is reported again.

    ./.venv/bin/python watchdog.py [--once]
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data" / "meetings"
STATE = APP_DIR / "data" / "watchdog_state.json"
LOG = APP_DIR / "data" / "watchdog.log"
BASE = "http://127.0.0.1:8377"
AGENT = "io.localfellow.server"   # bundle id predates the rename; see CHANGELOG
REPEAT_AFTER_S = 6 * 3600         # re-notify about an unfixed fault twice a day


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(title: str, message: str):
    """macOS notification — the only channel that reaches the user when the
    browser is closed."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title '
             f'{json.dumps(title)} sound name "Basso"'],
            capture_output=True, timeout=15)
    except Exception as e:
        log(f"notification failed: {e}")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict):
    try:
        STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass


def report(key: str, title: str, message: str):
    """Notify unless we already reported this exact fault recently."""
    st = _state()
    last = st.get(key, 0)
    if time.time() - last < REPEAT_AFTER_S:
        return
    notify(title, message)
    st[key] = time.time()
    _save(st)


def clear(key: str):
    st = _state()
    if key in st:
        st.pop(key)
        _save(st)


def get(path: str, timeout=8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def server_up() -> bool:
    try:
        get("/api/version", timeout=6)
        return True
    except Exception:
        return False


PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT}.plist"


def fix_server() -> bool:
    log("server not answering — restarting the LaunchAgent")
    dom = f"gui/{os.getuid()}"
    r = subprocess.run(["launchctl", "kickstart", "-k", f"{dom}/{AGENT}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        # kickstart only works on a LOADED service; if the agent was unloaded
        # (or never loaded after a reboot) it must be bootstrapped back in
        log(f"kickstart failed ({r.stderr.strip()[:80]}) — bootstrapping the agent")
        if PLIST.exists():
            subprocess.run(["launchctl", "bootstrap", dom, str(PLIST)],
                           capture_output=True, timeout=30)
        else:
            log(f"LaunchAgent plist missing at {PLIST}")
    for _ in range(10):
        time.sleep(6)
        if server_up():
            log("server is back")
            return True
    return False


def fix_ollama() -> bool:
    log("Ollama not answering — starting it")
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(8):
        time.sleep(4)
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4)
            log("Ollama is back")
            return True
        except Exception:
            pass
    return False


def restart_stuck(ids: list[str]):
    for mid in ids:
        d = DATA / mid
        if not d.exists():
            continue
        log(f"restarting worker for stuck meeting {mid}")
        subprocess.Popen([sys.executable, str(APP_DIR / "worker.py"), str(d)],
                         cwd=str(APP_DIR), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def check() -> bool:
    """One pass. Returns True if everything is healthy at the end."""
    if not server_up():
        if fix_server():
            report("server_restarted", "Baruch restarted",
                   "The server had stopped and was restarted automatically.")
        else:
            report("server_down", "Baruch is DOWN",
                   "The server is not running and could not be restarted. "
                   "Recording is unavailable.")
            log("server still down after restart attempt")
            return False
    clear("server_down")

    try:
        h = get("/api/health", timeout=20)
    except Exception as e:
        log(f"health endpoint unreachable: {e}")
        return False

    checks = h.get("checks", {})

    if not checks.get("ollama"):
        if fix_ollama():
            report("ollama_restarted", "Ollama restarted",
                   "Ollama had stopped; it was restarted. AI notes work again.")
        else:
            report("ollama_down", "Baruch: AI notes unavailable",
                   "Ollama is not running and could not be started. "
                   "Recordings will transcribe but produce NO AI note.")
    else:
        clear("ollama_down")

    if not checks.get("whisper"):
        report("whisper_missing", "Baruch: transcription unavailable",
               "whisper-cli was not found. Recordings cannot be transcribed.")
    else:
        clear("whisper_missing")

    free = checks.get("disk_free_gb")
    if isinstance(free, (int, float)) and free < 5:
        report("disk_low", "Baruch: low disk space",
               f"Only {free} GB free. Recordings may fail.")
    else:
        clear("disk_low")

    stuck = checks.get("stuck_meetings") or []
    if stuck:
        restart_stuck(stuck)
        report("stuck", "Baruch: processing was stuck",
               f"{len(stuck)} meeting(s) had stalled; processing was restarted.")
    else:
        clear("stuck")

    # re-read AFTER the repairs above, or the verdict reports the fault we
    # just fixed and every successful repair looks like a failure
    try:
        h = get("/api/health", timeout=20)
    except Exception:
        pass
    healthy = h.get("ok", False)
    log("healthy" if healthy else f"problems: {'; '.join(h.get('problems', []))}")
    return healthy


def main() -> int:
    healthy = check()
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
