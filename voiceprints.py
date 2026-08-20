"""Persistent speaker identification ("recognize my voice every time").

Voiceprints are 512-dim ERes2Net embeddings stored per person in
data/voiceprints.json. Two enrollment paths:
  1. explicit — record ~20 s via /api/enroll_voice;
  2. implicit — every all-lines speaker rename harvests prints from that
     meeting's audio, so corrections keep making recognition better.
Matching is conservative (default cosine >= 0.70, calibrated 2026-07-30:
same-voice ~0.92, different-voice <= 0.60): an uncertain voice stays
"Speaker N" rather than risking a wrong name."""
import json
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
import sherpa_onnx

APP_DIR = Path(__file__).resolve().parent
EMB_MODEL = APP_DIR / "models/embedding.onnx"
STORE = APP_DIR / "data" / "voiceprints.json"
MAX_PRINTS_PER_PERSON = 6
MIN_SEG_S, MAX_SEG_S = 2.0, 15.0
DEFAULT_THRESHOLD = 0.70


@lru_cache(maxsize=1)
def _extractor():
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)))


def _threshold() -> float:
    f = APP_DIR / "data" / "settings.json"
    if f.exists():
        return float(json.loads(f.read_text()).get("voice_threshold",
                                                   DEFAULT_THRESHOLD))
    return DEFAULT_THRESHOLD


def _load() -> dict:
    """Store format: {name: {"vecs": [...], "sources": [...], "updated": epoch}}.
    Transparently upgrades the legacy {name: [vecs]} format."""
    if not STORE.exists():
        return {}
    data = json.loads(STORE.read_text(encoding="utf-8"))
    for name, val in list(data.items()):
        if isinstance(val, list):  # legacy
            data[name] = {"vecs": val, "sources": ["enrolled"] * len(val),
                          "updated": 0}
    return data


def _save(prints: dict):
    STORE.write_text(json.dumps(prints), encoding="utf-8")


def list_people() -> list[dict]:
    """Who has stored voiceprints, how many, and how they were learned."""
    out = []
    for name, rec in _load().items():
        srcs = rec.get("sources", [])
        out.append({"name": name, "prints": len(rec.get("vecs", [])),
                    "enrolled": srcs.count("enrolled"),
                    "learned": srcs.count("learned"),
                    "updated": rec.get("updated", 0)})
    out.sort(key=lambda p: p["name"].lower())
    return out


def delete_person(name: str) -> bool:
    prints = _load()
    if name in prints:
        del prints[name]
        _save(prints)
        return True
    return False


def match_sample(samples: np.ndarray) -> tuple[str | None, float]:
    """Identify one audio clip against all stored prints (for 'test my voice')."""
    prints = _load()
    if not prints:
        return None, 0.0
    emb = _embed(samples)
    best_name, best = None, 0.0
    for name, rec in prints.items():
        for v in rec.get("vecs", []):
            s = _cos(emb, v)
            if s > best:
                best_name, best = name, s
    return best_name, best


def _embed(samples: np.ndarray) -> list[float]:
    ex = _extractor()
    st = ex.create_stream()
    st.accept_waveform(16000, samples)
    st.input_finished()
    return list(ex.compute(st))


def _cos(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _read_wav16(wav: Path) -> np.ndarray:
    with wave.open(str(wav)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0


def _speaker_samples(wav16: Path, segs: list[dict], speaker: str) -> list[np.ndarray]:
    """Longest clean segments (2-15 s each, up to 5) for one speaker."""
    audio = _read_wav16(wav16)
    windows = [(s["start_ms"], s["end_ms"]) for s in segs
               if s.get("speaker") == speaker
               and (s["end_ms"] - s["start_ms"]) / 1000.0 >= MIN_SEG_S]
    windows.sort(key=lambda w: w[1] - w[0], reverse=True)
    out = []
    for start_ms, end_ms in windows[:5]:
        end_ms = min(end_ms, start_ms + int(MAX_SEG_S * 1000))
        clip = audio[start_ms * 16: end_ms * 16]
        if len(clip) >= MIN_SEG_S * 16000:
            out.append(clip)
    return out


def enroll_from_samples(name: str, samples: list[np.ndarray],
                        source: str = "enrolled") -> int:
    """Add voiceprints for `name`. Returns how many prints were stored."""
    if not samples:
        return 0
    import time
    prints = _load()
    rec = prints.get(name, {"vecs": [], "sources": [], "updated": 0})
    enrolled = [v for v, s in zip(rec["vecs"], rec["sources"]) if s == "enrolled"]
    stored = 0
    for clip in samples:
        emb = _embed(clip)
        # anti-poisoning: a rename can be wrong — never let 'learned' prints
        # drift an explicitly enrolled identity toward a different voice
        if source == "learned" and enrolled and \
                max(_cos(emb, v) for v in enrolled) < 0.45:
            continue
        rec["vecs"].append(emb)
        rec["sources"].append(source)
        stored += 1
    if not stored:
        return 0
    rec["vecs"] = rec["vecs"][-MAX_PRINTS_PER_PERSON:]
    rec["sources"] = rec["sources"][-MAX_PRINTS_PER_PERSON:]
    rec["updated"] = int(time.time())
    prints[name] = rec
    _save(prints)
    return stored


def harvest_from_meeting(mdir: Path, speaker: str, name: str) -> int:
    """Implicit enrollment: after a rename, learn `name`'s voice from the
    meeting audio segments previously labelled `speaker`."""
    segs_f = mdir / "transcript.json"
    if not segs_f.exists():
        return 0
    segs = json.loads(segs_f.read_text(encoding="utf-8"))
    # note: transcript already carries the NEW name after rename
    wav16 = _meeting_wav16(mdir)
    if wav16 is None:
        return 0
    return enroll_from_samples(name, _speaker_samples(wav16, segs, name),
                               source="learned")


def _meeting_wav16(mdir: Path) -> Path | None:
    src = mdir / "meeting.wav"
    if not src.exists():
        return None
    dst = mdir / "meeting.16k.wav"
    if not dst.exists():
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(src), "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(dst)])
    return dst if dst.exists() else None


def has_prints(name: str) -> bool:
    return bool(_load().get(name, {}).get("vecs"))


def identify(wav16: Path, segs: list[dict], log=None):
    """Auto-label diarized speakers whose voice matches a stored print.
    Renames labels in `segs` in place. Returns (renamed, details) where
    details = {label: (best_candidate_name, best_score)} for every cluster."""
    details: dict[str, tuple] = {}
    prints = _load()
    if not prints or not EMB_MODEL.exists():
        return {}, details
    threshold = _threshold()
    unknown = sorted({s["speaker"] for s in segs
                      if s.get("speaker", "").startswith("Speaker ")
                      or s.get("speaker") == "Others"})
    renamed = {}
    for label in unknown:
        clips = _speaker_samples(wav16, segs, label)
        if not clips:
            continue
        # best average similarity across this speaker's clips, per person
        clip_embs = [_embed(c) for c in clips]
        best_name, best_score = None, 0.0
        for name, rec in prints.items():
            vecs = rec.get("vecs", [])
            if not vecs:
                continue
            sims = [max(_cos(ce, v) for v in vecs) for ce in clip_embs]
            score = sum(sims) / len(sims)
            if score > best_score:
                best_name, best_score = name, score
        details[label] = (best_name, best_score)
        # far-field diarization over-segments one voice into many clusters, so
        # several clusters MAY map to the same person. Tiny clusters (< 5 s)
        # are never named — noise blips match junk prints too easily.
        dur = sum((s["end_ms"] - s["start_ms"]) for s in segs
                  if s["speaker"] == label) / 1000.0
        if best_name and best_score >= threshold and dur >= 5.0:
            for s in segs:
                if s["speaker"] == label:
                    s["speaker"] = best_name
            renamed[label] = best_name
        if log:
            log(f"voiceprint {label}: best={best_name} score={best_score:.2f} "
                f"dur={dur:.0f}s {'-> ' + best_name if label in renamed else '(kept)'}")

    # dominant-speaker absorption: if one identified person owns most of the
    # track, unnamed clusters that still resemble them (relaxed threshold)
    # are almost certainly the same voice fragmented by far-field audio.
    # Named speech counts whether it was named in this pass or previously.
    if True:
        total = sum(s["end_ms"] - s["start_ms"] for s in segs) / 1000.0
        by_name: dict[str, float] = {}
        for s in segs:
            if not s["speaker"].startswith("Speaker ") and s["speaker"] != "Others":
                by_name[s["speaker"]] = by_name.get(s["speaker"], 0) + \
                    (s["end_ms"] - s["start_ms"]) / 1000.0
        if by_name and total:
            dominant = max(by_name, key=by_name.get)
            named_total = sum(by_name.values())
            # dominance is measured among NAMED speech (a 10 s stray name must
            # not veto absorbing a 40-minute lecturer), with a floor on total
            if by_name[dominant] / named_total >= 0.8 and \
                    by_name[dominant] / total >= 0.15:
                absorbed = []
                for label, (cand, score) in details.items():
                    if label in renamed or cand != dominant or score < 0.45:
                        continue
                    for s in segs:
                        if s["speaker"] == label:
                            s["speaker"] = dominant
                    renamed[label] = dominant
                    absorbed.append(label)
                if absorbed and log:
                    log(f"absorbed {len(absorbed)} fragment clusters into "
                        f"{dominant} (dominant-speaker rule)")
    return renamed, details


# --- speaker consolidation -------------------------------------------------
# Diarization over-splits a single voice across a long recording, and merging
# two parts doubles the labels because each part was diarized independently.
# A 2 h 14 m lecture with essentially one speaker came out as 39 speakers, and
# a 2 h 42 m one left 40+ slivers beside a correctly-named 92% speaker. Both
# meant renaming by hand, repeatedly.
MERGE_THRESHOLD = 0.58   # looser than identification (0.70): the question is
                         # "same voice as that cluster", not "is this the
                         # enrolled person", and the cost of a wrong merge is
                         # far lower than 39 labels for one lecturer
DOMINANT_SHARE = 0.55    # one voice holding this much of the speech means a
                         # lecture, not a discussion
SLIVER_SHARE = 0.02      # clusters this small beside a dominant voice are
SLIVER_SECS = 90         # fragmentation, not participants


def _label_seconds(segs: list[dict]) -> dict:
    out: dict[str, float] = {}
    for s in segs:
        sp = s.get("speaker", "")
        out[sp] = out.get(sp, 0.0) + max(0, (s.get("end_ms", 0) -
                                             s.get("start_ms", 0))) / 1000
    return out


def consolidate_speakers(mdir: Path, segs: list[dict], log=None) -> dict:
    """Merge diarized clusters that are the same voice, then absorb leftover
    slivers into the dominant speaker. Mutates `segs`; returns {old: new}.

    Named speakers are never absorbed or merged away — a real Q&A voice that
    has been identified must survive, and only unnamed "Speaker N" clusters
    are candidates."""
    def _log(m):
        if log:
            log(mdir, f"consolidate: {m}")

    secs = _label_seconds(segs)
    unnamed = [l for l in secs if l.startswith("Speaker ") or l == "Others"]
    if len(unnamed) < 2:
        return {}

    mapping: dict[str, str] = {}
    wav16 = _meeting_wav16(mdir)
    if wav16 and EMB_MODEL.exists():
        try:
            wav = _read_wav16(wav16)
            cent = {}
            for lab in unnamed:
                samples = _speaker_samples(wav16, segs, lab)
                if not samples:
                    continue
                vecs = [_embed(s) for s in samples[:6]]
                if vecs:
                    cent[lab] = np.mean(np.array(vecs), axis=0)
            # agglomerative: biggest cluster absorbs whoever matches it
            order = sorted(cent, key=lambda l: -secs.get(l, 0))
            for i, keep in enumerate(order):
                if mapping.get(keep):
                    continue
                for other in order[i + 1:]:
                    if other in mapping or other == keep:
                        continue
                    if _cos(cent[keep], cent[other]) >= MERGE_THRESHOLD:
                        mapping[other] = keep
            if mapping:
                _log(f"voice-matched {len(mapping)} cluster(s) into "
                     f"{len(set(mapping.values()))}")
        except Exception as e:
            _log(f"voice matching skipped: {e}")

    # apply, then absorb slivers under a dominant voice
    for s in segs:
        sp = s.get("speaker", "")
        if sp in mapping:
            s["speaker"] = mapping[sp]

    secs = _label_seconds(segs)
    total = sum(secs.values()) or 1
    top = max(secs, key=lambda l: secs[l])
    if secs[top] / total >= DOMINANT_SHARE:
        absorbed = 0
        for lab, sec in list(secs.items()):
            if lab == top or not (lab.startswith("Speaker ") or lab == "Others"):
                continue
            if sec / total < SLIVER_SHARE and sec < SLIVER_SECS:
                for s in segs:
                    if s.get("speaker") == lab:
                        s["speaker"] = top
                mapping[lab] = top
                absorbed += 1
        if absorbed:
            _log(f"absorbed {absorbed} sliver(s) into {top} "
                 f"({100*secs[top]/total:.0f}% of speech)")
    return mapping
