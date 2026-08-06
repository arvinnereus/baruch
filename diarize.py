"""Offline speaker diarization via sherpa-onnx (pyannote segmentation 3.0 +
3D-Speaker ERes2Net embeddings). Input: 16 kHz mono WAV. Output: list of
(start_s, end_s, "Speaker N") with speakers numbered by first appearance."""
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
import sherpa_onnx

APP_DIR = Path(__file__).resolve().parent
SEG_MODEL = APP_DIR / "models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMB_MODEL = APP_DIR / "models/embedding.onnx"


@lru_cache(maxsize=1)
def _engine():
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(SEG_MODEL))),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(EMB_MODEL)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1,
                                                    threshold=0.6),
        min_duration_on=0.3,
        min_duration_off=0.5)
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def available() -> bool:
    return SEG_MODEL.exists() and EMB_MODEL.exists()


def diarize_wav(wav_path: Path) -> list[tuple[float, float, str]]:
    """Diarize a 16 kHz mono s16le WAV. Returns [] on any failure."""
    try:
        with wave.open(str(wav_path)) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1
            samples = np.frombuffer(w.readframes(w.getnframes()),
                                    dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < 16000:  # under a second — nothing to do
            return []
        segments = _engine().process(samples).sort_by_start_time()
    except Exception:
        return []
    # renumber speakers by first appearance: 0,1,2… -> Speaker 1,2,3…
    order: dict[int, int] = {}
    out = []
    for seg in segments:
        if seg.speaker not in order:
            order[seg.speaker] = len(order) + 1
        out.append((seg.start, seg.end, f"Speaker {order[seg.speaker]}"))
    return out


def assign_speakers(whisper_segs: list[dict], diar: list[tuple],
                    fallback: str) -> None:
    """Label each whisper segment with the diarized speaker that overlaps it
    most (in place). Falls back to the previous label, then `fallback`."""
    if not diar:
        return
    last = fallback
    for seg in whisper_segs:
        s, e = seg["start_ms"] / 1000.0, seg["end_ms"] / 1000.0
        overlap: dict[str, float] = {}
        for ds, de, spk in diar:
            ov = min(e, de) - max(s, ds)
            if ov > 0:
                overlap[spk] = overlap.get(spk, 0.0) + ov
        if overlap:
            last = max(overlap, key=overlap.get)
        seg["speaker"] = last
