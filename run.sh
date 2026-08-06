#!/bin/bash
# LocalFellow — one-command launcher.
#   ./run.sh            # starts everything and opens the app in your browser
set -e
cd "$(dirname "$0")"
mkdir -p data/meetings

# --- Whisper model (app/models, or an existing sibling install) ---
mkdir -p models
if [ ! -f models/ggml-large-v3-turbo.bin ] && [ ! -f models/ggml-large-v3.bin ] \
   && [ ! -f "$HOME/ClearCaption/models/ggml-large-v3-turbo.bin" ] \
   && [ ! -f "$HOME/ClearCaption/models/ggml-large-v3.bin" ]; then
  echo "Downloading Whisper model (large-v3-turbo, ~1.6 GB, one-time)…"
  curl -L -o models/ggml-large-v3-turbo.bin \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
fi

# --- speaker diarization + voiceprint models (one-time, ~46 MB) ---
if [ ! -f models/embedding.onnx ]; then
  echo "Downloading speaker models…"
  curl -sL -o models/seg.tar.bz2 \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
  tar xjf models/seg.tar.bz2 -C models && rm models/seg.tar.bz2
  curl -sL -o models/embedding.onnx \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
fi

# --- systemaudio tool (botless Zoom/Meet capture) ---
if [ ! -x systemaudio ]; then
  echo "Building systemaudio (ScreenCaptureKit)…"
  swiftc -O -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia \
    systemaudio.swift -o systemaudio
fi

# --- voice-processed mic capture (echo cancellation for online meetings) ---
if [ ! -x voicemic ] || [ voicemic.swift -nt voicemic ]; then
  echo "Building voicemic…"
  swiftc -O voicemic.swift -o voicemic
fi

# --- menu-bar companion (system-wide record status/control) ---
if [ ! -x menubar ] || [ menubar.swift -nt menubar ]; then
  echo "Building menubar…"
  swiftc -O menubar.swift -o menubar
fi
pgrep -f "$PWD/menubar" > /dev/null || ("$PWD/menubar" > /dev/null 2>&1 &)

# --- Python env ---
if [ ! -d .venv ]; then
  echo "Creating Python env…"
  python3 -m venv .venv
  .venv/bin/pip -q install "fastapi>=0.110" "uvicorn>=0.29" "python-multipart>=0.0.9" "python-docx>=1.1" "sherpa-onnx>=1.13" "numpy>=2" "mcp>=2"
fi

# --- Ollama (AI notes) ---
if command -v ollama >/dev/null; then
  if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null; then
    echo "Starting ollama…"
    (ollama serve > /dev/null 2>&1 &)
    sleep 2
  fi
else
  echo "⚠️  ollama not installed — transcripts will work, AI notes won't."
fi

PORT="${PORT:-8377}"
echo "LocalFellow → http://127.0.0.1:$PORT"
(sleep 1.2 && open "http://127.0.0.1:$PORT") &
exec .venv/bin/uvicorn server:app --host 127.0.0.1 --port "$PORT"
