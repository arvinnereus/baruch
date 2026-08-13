"""Auto-save AI notes into the Obsidian Second Brain vault.

Notes land in `07 - Meetings/YYYY-MM-DD — Title.md` with frontmatter, synced
to every device by the vault's existing Google Drive pipeline — readable in
Obsidian mobile even when Caleb is asleep. Override the folder with
`obsidian_dir` in data/settings.json; export skips silently if the vault
isn't reachable (e.g. Drive not mounted)."""
import json
import re
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _find_vault() -> Path | None:
    """Obsidian vault on a Google Drive desktop mount (any account)."""
    base = Path.home() / "Library/CloudStorage"
    if base.exists():
        for p in sorted(base.glob("GoogleDrive-*")):
            for v in (p / "My Drive" / "Obsidian").glob("*"):
                if v.is_dir():
                    return v
    return None


VAULT = _find_vault()
DEFAULT_DIR = (VAULT / "07 - Meetings") if VAULT else None


def _out_dir() -> Path | None:
    f = APP_DIR / "data" / "settings.json"
    if f.exists():
        custom = json.loads(f.read_text(encoding="utf-8")).get("obsidian_dir")
        if custom:
            return Path(custom)
    return DEFAULT_DIR  # None when no vault is detected — export skips


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "-", name).strip()[:80] or "Meeting"


def export_note(meta: dict, note: dict, note_md: str, mdir: Path) -> Path | None:
    out_dir = _out_dir()
    if out_dir is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)

    day = time.strftime("%Y-%m-%d", time.localtime(meta.get("created_at", 0)))
    speakers = []
    tf = mdir / "transcript.json"
    if tf.exists():
        seen = set()
        for s in json.loads(tf.read_text(encoding="utf-8")):
            sp = s.get("speaker", "")
            if sp and not sp.startswith("Speaker ") and sp not in seen:
                seen.add(sp)
                speakers.append(sp)

    fname = f"{day} — {_safe(meta.get('title', 'Meeting'))}.md"
    path = out_dir / fname
    # same-day title clash with a DIFFERENT meeting → disambiguate by id
    if path.exists():
        head = path.read_text(encoding="utf-8")[:400]
        if f"meeting_id: {meta['id']}" not in head:
            path = out_dir / f"{day} — {_safe(meta.get('title', 'Meeting'))} ({meta['id'][:6]}).md"

    fm = ["---",
          f"date: {day}",
          "tags: [meeting, baruch]",
          f"meeting_id: {meta['id']}",
          f"template: {note.get('template', 'general')}",
          f"duration_min: {meta.get('duration_s', 0) // 60}"]
    if speakers:
        fm.append("speakers: [" + ", ".join(speakers) + "]")
    fm.append("---")
    body = "\n".join(fm) + "\n\n" + note_md.strip() + \
        "\n\n---\n*Recorded with Baruch · transcript & audio on Caleb*\n"
    path.write_text(body, encoding="utf-8")
    return path
